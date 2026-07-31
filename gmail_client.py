import os
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from config import settings
from credential_source import CredentialSource, FileTokenSource, SCOPES

logger = logging.getLogger("email_triage.gmail")

# HTTP statuses worth retrying with backoff (rate limiting / transient server errors), as opposed
# to permanent failures (404 deleted message, 400 bad request, auth errors) that should not retry.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Gmail counts every sub-request in an HTTP batch against the per-user-per-minute quota
# individually -- batching only saves round-trips, not quota. For a small (unread-only) backlog
# this never mattered, but a full-mailbox download can walk hundreds of consecutive 100-message
# batches with nothing to slow it down, blowing through the per-minute quota within the first
# minute. This is a proactive pace limit between successive batch calls (not the reactive
# retry-with-backoff below, which only kicks in after a batch has already been rate-limited).
_INTER_BATCH_DELAY_SECONDS = 2.0


def _http_status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) if resp is not None else None

class GmailClient:
    # Class-level default so test doubles built via GmailClient.__new__ (bypassing __init__)
    # still see a well-defined starting value; real instances shadow it with their own once
    # _throttle_batch_call runs.
    _last_batch_call_at: Optional[float] = None

    def __init__(
        self,
        settings_instance: Optional[Any] = None,
        credential_source: Optional[CredentialSource] = None,
    ) -> None:
        self.settings = settings_instance if settings_instance else settings
        self._source: CredentialSource = credential_source or FileTokenSource(
            token_path=self.settings.gmail_token_path,
            credentials_path=self.settings.gmail_credentials_path,
            headless_mode=self.settings.headless_mode,
        )
        self.creds: Optional[Credentials] = None
        self.service = None
        self._authenticate()

    def _throttle_batch_call(self) -> None:
        """
        Ensures at least _INTER_BATCH_DELAY_SECONDS has elapsed since this client's last Gmail
        HTTP batch call (metadata or body) before letting the next one through -- across separate
        top-level calls to _fetch_metadata_batch/fetch_full_bodies_batch too, not just between
        chunks within one of them. A caller that chunks work across multiple invocations (e.g.
        the full-mailbox archive downloader) would otherwise burst two 100-message batches
        back-to-back at the seam between calls, with no pacing in between.
        """
        now = time.monotonic()
        if self._last_batch_call_at is not None:
            remaining = _INTER_BATCH_DELAY_SECONDS - (now - self._last_batch_call_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_batch_call_at = time.monotonic()

    def _authenticate(self) -> None:
        """Handles OAuth 2.0 authentication flow and loads/persists tokens via
        self._source (FileTokenSource by default -- see credential_source.py).
        """
        # Allow local HTTP redirect URIs (needed for container/headless OAuth flow)
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        try:
            self.creds = self._source.load()
            if self.creds:
                logger.info("Loaded existing Gmail credentials.")

            # If there are no (valid) credentials available, let the user log in.
            if not self.creds or not self.creds.valid:
                trigger_oauth = True
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    try:
                        logger.info("Gmail token expired. Attempting silent refresh...")
                        self.creds.refresh(Request())
                        trigger_oauth = False
                        logger.info("Gmail token refreshed successfully.")
                    except Exception as refresh_err:
                        logger.warning("Silent token refresh failed (%s). Falling back to full OAuth flow.", refresh_err)

                if trigger_oauth:
                    logger.info("No valid persistent token found or refresh failed. Initializing OAuth flow...")
                    self.creds = self._source.interactive_or_fail()

                # Save the credentials for the next run
                self._source.save(self.creds)
                logger.info("Gmail token persisted successfully.")

            self.service = build('gmail', 'v1', credentials=self.creds, cache_discovery=False)
            logger.info("Gmail API Service client successfully created.")
        except Exception as e:
            logger.error("Gmail authentication failure: %s", e, exc_info=True)
            raise

    def _fetch_metadata_batch(self, messages: List[Dict[str, Any]], max_retries: int = 4) -> List[Dict[str, Any]]:
        """
        Helper method to fetch metadata for multiple messages using Gmail HTTP Batching.
        Reduces roundtrips by batching up to 100 requests per batch call. Requests that fail
        with a transient error (429 rate limiting, 5xx) are retried with exponential backoff
        up to max_retries times before being given up on; permanent errors are logged and dropped.
        """
        if not self.service or not messages:
            return []

        results: List[Dict[str, Any]] = []
        permanently_failed_ids = set()

        def batch_callback(request_id, response, exception):
            if exception is not None:
                if _http_status(exception) not in _RETRYABLE_STATUS_CODES:
                    logger.error("Failed to fetch message metadata for %s: %s", request_id, exception)
                    permanently_failed_ids.add(request_id)
                return
            try:
                headers = response.get('payload', {}).get('headers', [])
                header_dict = {h['name']: h['value'] for h in headers}

                msg_id = response.get('id')
                message_id = header_dict.get('Message-ID', msg_id)
                from_str = header_dict.get('From', 'Unknown Sender')
                subject_str = header_dict.get('Subject', '(No Subject)')
                date_str = header_dict.get('Date', '')
                snippet_str = response.get('snippet', '')

                results.append({
                    'id': msg_id,
                    'message_id': message_id,
                    'sender': from_str,
                    'subject': subject_str,
                    'date': date_str,
                    'snippet': snippet_str,
                    'account': self.settings.gmail_account,
                    'raw_meta': response
                })
            except Exception as callback_err:
                logger.error("Error parsing batch response metadata: %s", callback_err)
                permanently_failed_ids.add(request_id)

        all_ids = [msg['id'] for msg in messages]
        chunk_size = 100
        pending_ids = list(all_ids)
        attempt = 0

        while pending_ids and attempt <= max_retries:
            if attempt > 0:
                delay = min(2 ** attempt, 30)
                logger.warning(
                    "Retrying %d Gmail metadata fetch(es) after rate limiting/transient error "
                    "(attempt %d/%d, backoff %ss)",
                    len(pending_ids), attempt, max_retries, delay,
                )
                time.sleep(delay)

            for i in range(0, len(pending_ids), chunk_size):
                self._throttle_batch_call()
                chunk_ids = pending_ids[i:i + chunk_size]
                try:
                    batch = self.service.new_batch_http_request(callback=batch_callback)
                    for mid in chunk_ids:
                        batch.add(
                            self.service.users().messages().get(
                                userId='me', id=mid, format='metadata',
                                metadataHeaders=['Message-ID', 'From', 'Subject', 'Date']
                            ),
                            request_id=mid,
                        )
                    batch.execute()
                except Exception as e:
                    if _http_status(e) in _RETRYABLE_STATUS_CODES:
                        logger.warning("Gmail metadata batch call rate-limited/transient error: %s", e)
                        continue
                    logger.error("Failed to execute Gmail metadata batch: %s. Falling back to sequential...", e)
                    # Fallback to sequential fetching for this chunk
                    for mid in chunk_ids:
                        try:
                            msg_meta = self.service.users().messages().get(
                                userId='me', id=mid, format='metadata',
                                metadataHeaders=['Message-ID', 'From', 'Subject', 'Date']
                            ).execute()
                            headers = msg_meta.get('payload', {}).get('headers', [])
                            header_dict = {h['name']: h['value'] for h in headers}
                            results.append({
                                'id': mid,
                                'message_id': header_dict.get('Message-ID', mid),
                                'sender': header_dict.get('From', 'Unknown Sender'),
                                'subject': header_dict.get('Subject', '(No Subject)'),
                                'date': header_dict.get('Date', ''),
                                'snippet': msg_meta.get('snippet', ''),
                                'account': self.settings.gmail_account,
                                'raw_meta': msg_meta
                            })
                        except Exception as fallback_err:
                            if _http_status(fallback_err) not in _RETRYABLE_STATUS_CODES:
                                logger.error("Sequential fallback failed for message %s: %s", mid, fallback_err)
                                permanently_failed_ids.add(mid)

            done_ids = {r['id'] for r in results}
            pending_ids = [mid for mid in pending_ids if mid not in done_ids and mid not in permanently_failed_ids]
            attempt += 1

        if pending_ids:
            logger.error(
                "Giving up on %d Gmail message(s) after %d retries due to persistent rate limiting/errors: %s",
                len(pending_ids), max_retries, pending_ids[:10],
            )

        # Sort results in the same order as input messages to preserve ordering
        msg_id_to_index = {mid: idx for idx, mid in enumerate(all_ids)}
        results.sort(key=lambda r: msg_id_to_index.get(r['id'], 99999))

        return results

    def _list_message_ids(self, query: str, max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Shared `messages.list` pagination helper -- bare {id, threadId} entries only, no
        per-message metadata fetch. Omits the `q` param entirely when `query` is empty, which
        (with no labelIds filter either) matches Gmail's own "All Mail" default scope: every
        message except Spam and Trash.
        """
        if not self.service:
            logger.error("Gmail service client is not initialized.")
            return []

        try:
            logger.info("Listing Gmail messages with query: '%s'", query or "(none -- all mail)")
            messages: List[Dict[str, Any]] = []
            page_token = None
            while True:
                list_params = {'userId': 'me'}
                if query:
                    list_params['q'] = query
                if page_token:
                    list_params['pageToken'] = page_token
                if max_results is not None:
                    list_params['maxResults'] = min(max_results - len(messages), 500)

                response = self.service.users().messages().list(**list_params).execute()
                messages.extend(response.get('messages', []))

                page_token = response.get('nextPageToken')
                if not page_token or (max_results is not None and len(messages) >= max_results):
                    break

            if max_results is not None:
                messages = messages[:max_results]

            return messages
        except Exception as e:
            logger.error("Failed to list Gmail messages: %s", e, exc_info=True)
            return []

    def list_unread_ids(
        self,
        query: str = "is:unread",
        max_results: Optional[int] = None,
        days: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Lists bare {id, threadId} entries for messages matching the query via `messages.list`
        pagination only -- no per-message metadata fetch. Cheap relative to metadata/full-body
        fetches, so callers that already know some of these ids (e.g. from a prior sync tick)
        can skip metadata fetching for them instead of always resolving the whole result set.
        """
        if days is not None and days > 0:
            query = f"{query} newer_than:{days}d"
        return self._list_message_ids(query, max_results)

    def list_all_ids(self, max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Lists bare {id, threadId} entries for EVERY message in the mailbox, for a one-time
        full-archive download -- unlike list_unread_ids, this is not scoped to unread.
        """
        return self._list_message_ids("", max_results)

    def fetch_unread_messages(
        self,
        query: str = "is:unread",
        max_results: Optional[int] = None,
        days: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetches metadata for unread messages matching the query.
        Uses format='metadata' and batch requests for efficiency.
        """
        messages = self.list_unread_ids(query=query, max_results=max_results, days=days)
        if not messages:
            logger.info("No new unread Gmail messages found matching query.")
            return []

        logger.info("Found %d unread messages. Fetching metadata using HTTP batching...", len(messages))
        return self._fetch_metadata_batch(messages)

    @staticmethod
    def _parse_full_message_body(msg: Dict[str, Any]) -> str:
        """Extracts plain-text (falling back to HTML, then snippet) body content from a
        format='full' Gmail message resource."""
        import base64

        def get_part_body(part: Dict[str, Any]) -> str:
            part_body = part.get('body', {})
            data = part_body.get('data', '')
            if data:
                return base64.urlsafe_b64decode(data.encode('utf-8')).decode('utf-8', errors='ignore')
            return ""

        payload = msg.get('payload', {})
        body = ""
        parts = payload.get('parts', [])
        if parts:
            for part in parts:
                if part.get('mimeType') == 'text/plain':
                    body += get_part_body(part)
                elif part.get('mimeType') == 'text/html' and not body:
                    body += get_part_body(part)
                elif 'parts' in part:
                    for subpart in part['parts']:
                        if subpart.get('mimeType') == 'text/plain':
                            body += get_part_body(subpart)
        else:
            body = get_part_body(payload)

        return body if body else msg.get('snippet', '')

    def fetch_full_body(self, msg_id: str, max_retries: int = 4) -> str:
        """Fetch the full body of a single message."""
        if not self.service:
            return ""
        try:
            logger.info("Escalating: Fetching full email payload for message %s", msg_id)
            attempt = 0
            while True:
                try:
                    msg = self.service.users().messages().get(userId='me', id=msg_id, format='full').execute()
                    break
                except Exception as e:
                    if attempt < max_retries and _http_status(e) in _RETRYABLE_STATUS_CODES:
                        delay = min(2 ** attempt, 30)
                        logger.warning(
                            "Gmail full-body fetch rate-limited/transient error for %s, retrying in %ss (attempt %d/%d)",
                            msg_id, delay, attempt + 1, max_retries,
                        )
                        time.sleep(delay)
                        attempt += 1
                        continue
                    raise

            return self._parse_full_message_body(msg)
        except Exception as e:
            logger.error("Failed to fetch full body for message %s: %s", msg_id, e)
            return ""

    def fetch_full_bodies_batch(self, msg_ids: List[str], max_retries: int = 4) -> Dict[str, str]:
        """
        Fetches full body content for multiple messages using Gmail HTTP Batching (up to 100 per
        HTTP round trip), instead of one sequential request per message. Note this only reduces
        network round-trip time -- Gmail's quota system still counts each sub-request individually,
        so this does not reduce API quota usage or 429 risk (retry-with-backoff still applies).
        Returns a dict of {msg_id: body_text}; ids that ultimately fail are simply omitted.
        """
        if not self.service or not msg_ids:
            return {}

        results: Dict[str, str] = {}
        permanently_failed_ids = set()

        def batch_callback(request_id, response, exception):
            if exception is not None:
                if _http_status(exception) not in _RETRYABLE_STATUS_CODES:
                    logger.error("Failed to fetch full body for %s: %s", request_id, exception)
                    permanently_failed_ids.add(request_id)
                return
            try:
                results[request_id] = self._parse_full_message_body(response)
            except Exception as callback_err:
                logger.error("Error parsing batch full-body response for %s: %s", request_id, callback_err)
                permanently_failed_ids.add(request_id)

        chunk_size = 100
        pending_ids = list(msg_ids)
        attempt = 0

        while pending_ids and attempt <= max_retries:
            if attempt > 0:
                delay = min(2 ** attempt, 30)
                logger.warning(
                    "Retrying %d Gmail full-body fetch(es) after rate limiting/transient error "
                    "(attempt %d/%d, backoff %ss)",
                    len(pending_ids), attempt, max_retries, delay,
                )
                time.sleep(delay)

            for i in range(0, len(pending_ids), chunk_size):
                self._throttle_batch_call()
                chunk_ids = pending_ids[i:i + chunk_size]
                try:
                    batch = self.service.new_batch_http_request(callback=batch_callback)
                    for mid in chunk_ids:
                        batch.add(
                            self.service.users().messages().get(userId='me', id=mid, format='full'),
                            request_id=mid,
                        )
                    batch.execute()
                except Exception as e:
                    if _http_status(e) in _RETRYABLE_STATUS_CODES:
                        logger.warning("Gmail full-body batch call rate-limited/transient error: %s", e)
                        continue
                    logger.error("Failed to execute Gmail full-body batch: %s. Falling back to sequential...", e)
                    for mid in chunk_ids:
                        body = self.fetch_full_body(mid, max_retries=max_retries)
                        if body:
                            results[mid] = body
                        else:
                            permanently_failed_ids.add(mid)

            pending_ids = [mid for mid in pending_ids if mid not in results and mid not in permanently_failed_ids]
            attempt += 1

        if pending_ids:
            logger.error(
                "Giving up on %d Gmail full-body fetch(es) after %d retries due to persistent rate limiting/errors: %s",
                len(pending_ids), max_retries, pending_ids[:10],
            )

        return results

    def mark_as_read(self, msg_ids: List[str]) -> bool:
        """Mark a list of Gmail internal message IDs as read by removing the UNREAD label."""
        if not self.service or not msg_ids:
            return False
        try:
            logger.info("Marking %d Gmail messages as read...", len(msg_ids))
            self.service.users().messages().batchModify(
                userId='me',
                body={
                    'ids': msg_ids,
                    'removeLabelIds': ['UNREAD']
                }
            ).execute()
            return True
        except Exception as e:
            logger.error("Failed to mark Gmail messages as read: %s", e)
            return False

    def search_messages(self, query: str) -> List[Dict[str, Any]]:
        """
        Searches Gmail messages matching a specific query.
        Uses format='metadata' and batch requests for efficiency.
        """
        if not self.service:
            logger.error("Gmail service client is not initialized.")
            return []

        try:
            logger.info("Searching Gmail messages with query: '%s'", query)
            response = self.service.users().messages().list(userId='me', q=query).execute()
            messages = response.get('messages', [])

            if not messages:
                logger.info("No Gmail messages found matching search query.")
                return []

            logger.info("Found %d matching messages. Fetching metadata using HTTP batching...", len(messages))
            return self._fetch_metadata_batch(messages)
        except Exception as e:
            logger.error("Failed to search Gmail messages: %s", e, exc_info=True)
            return []

    def _find_message(self, message_id_or_id: str) -> Dict[str, Any]:
        """
        Finds a message by its internal ID or its RFC 2822 Message-ID.
        Returns the message metadata.
        """
        if not self.service:
            raise ValueError("Gmail service client is not initialized.")
        
        # 1. Try treating it as internal ID first
        try:
            msg = self.service.users().messages().get(
                userId='me', id=message_id_or_id, format='metadata',
                metadataHeaders=['Message-ID', 'From', 'Subject', 'Date', 'Reply-To']
            ).execute()
            return msg
        except Exception:
            # 2. Try querying by RFC Message-ID
            query = f"rfc822msgid:{message_id_or_id}"
            response = self.service.users().messages().list(userId='me', q=query).execute()
            messages = response.get('messages', [])
            if not messages and not message_id_or_id.startswith("<"):
                query = f"rfc822msgid:<{message_id_or_id}>"
                response = self.service.users().messages().list(userId='me', q=query).execute()
                messages = response.get('messages', [])
            
            if not messages:
                raise ValueError(f"Message not found in Gmail with ID or Message-ID: {message_id_or_id}")
            
            msg = self.service.users().messages().get(
                userId='me', id=messages[0]['id'], format='metadata',
                metadataHeaders=['Message-ID', 'From', 'Subject', 'Date', 'Reply-To']
            ).execute()
            return msg

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Creates a draft in Gmail.
        """
        if not self.service:
            raise ValueError("Gmail service client is not initialized.")
        try:
            from email.message import EmailMessage
            import base64

            mime_msg = EmailMessage()
            mime_msg["To"] = to
            mime_msg["Subject"] = subject
            mime_msg["From"] = self.settings.gmail_account
            mime_msg.set_content(body)

            if in_reply_to:
                mime_msg["In-Reply-To"] = in_reply_to
            if references:
                mime_msg["References"] = references

            raw_bytes = mime_msg.as_bytes()
            raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

            draft_body = {
                "message": {
                    "raw": raw_b64
                }
            }
            if thread_id:
                draft_body["message"]["threadId"] = thread_id

            logger.info("Creating Gmail draft (To: %s, Subject: %s)", to, subject)
            draft = self.service.users().drafts().create(userId="me", body=draft_body).execute()
            return draft
        except Exception as e:
            logger.error("Failed to create Gmail draft: %s", e, exc_info=True)
            raise

    def create_reply_draft(self, message_id: str, body: str) -> Dict[str, Any]:
        """
        Creates a draft reply to an existing message.
        """
        parent_msg = self._find_message(message_id)
        thread_id = parent_msg.get('threadId')
        
        headers = parent_msg.get('payload', {}).get('headers', [])
        header_dict = {h['name'].lower(): h['value'] for h in headers}
        
        to = header_dict.get('reply-to') or header_dict.get('from')
        if not to:
            raise ValueError(f"Could not identify the sender of message {message_id}")
            
        subject = header_dict.get('subject', '')
        if subject and not subject.lower().startswith('re:'):
            subject = f"Re: {subject}"
        elif not subject:
            subject = "Re: (No Subject)"
            
        parent_rfc_msg_id = header_dict.get('message-id')
        references = header_dict.get('references', '')
        
        if parent_rfc_msg_id:
            if references:
                references = f"{references} {parent_rfc_msg_id}"
            else:
                references = parent_rfc_msg_id
                
        return self.create_draft(
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=parent_rfc_msg_id,
            references=references
        )

    def send_reply(self, message_id: str, body: str) -> Dict[str, Any]:
        """
        Sends a reply to an existing message directly.
        """
        if not self.service:
            raise ValueError("Gmail service client is not initialized.")
        try:
            parent_msg = self._find_message(message_id)
            thread_id = parent_msg.get('threadId')
            
            headers = parent_msg.get('payload', {}).get('headers', [])
            header_dict = {h['name'].lower(): h['value'] for h in headers}
            
            to = header_dict.get('reply-to') or header_dict.get('from')
            if not to:
                raise ValueError(f"Could not identify the sender of message {message_id}")
                
            subject = header_dict.get('subject', '')
            if subject and not subject.lower().startswith('re:'):
                subject = f"Re: {subject}"
            elif not subject:
                subject = "Re: (No Subject)"
                
            parent_rfc_msg_id = header_dict.get('message-id')
            references = header_dict.get('references', '')
            
            if parent_rfc_msg_id:
                if references:
                    references = f"{references} {parent_rfc_msg_id}"
                else:
                    references = parent_rfc_msg_id

            from email.message import EmailMessage
            import base64

            mime_msg = EmailMessage()
            mime_msg["To"] = to
            mime_msg["Subject"] = subject
            mime_msg["From"] = self.settings.gmail_account
            mime_msg.set_content(body)

            if parent_rfc_msg_id:
                mime_msg["In-Reply-To"] = parent_rfc_msg_id
            if references:
                mime_msg["References"] = references

            raw_bytes = mime_msg.as_bytes()
            raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")

            body_data = {
                "raw": raw_b64
            }
            if thread_id:
                body_data["threadId"] = thread_id

            logger.info("Sending Gmail reply to message %s (To: %s)", message_id, to)
            sent_msg = self.service.users().messages().send(userId="me", body=body_data).execute()
            return sent_msg
        except Exception as e:
            logger.error("Failed to send Gmail reply: %s", e, exc_info=True)
            raise

