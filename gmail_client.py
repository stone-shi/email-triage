import os
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from config import settings

logger = logging.getLogger("email_triage.gmail")

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

# HTTP statuses worth retrying with backoff (rate limiting / transient server errors), as opposed
# to permanent failures (404 deleted message, 400 bad request, auth errors) that should not retry.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _http_status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) if resp is not None else None

class GmailClient:
    def __init__(self, settings_instance: Optional[Any] = None) -> None:
        self.settings = settings_instance if settings_instance else settings
        self.creds: Optional[Credentials] = None
        self.service = None
        self._authenticate()

    def _authenticate(self) -> None:
        """Handles OAuth 2.0 authentication flow and loads/persists tokens."""
        # Allow local HTTP redirect URIs (needed for container/headless OAuth flow)
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
        try:
            if self.settings.gmail_token_path.exists():
                self.creds = Credentials.from_authorized_user_file(
                    str(self.settings.gmail_token_path), SCOPES
                )
                logger.info("Loaded Gmail credentials from persistent token file.")

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
                    logger.info("No valid persistent token found or refresh failed. Initializing OAuth local server flow...")
                    if not self.settings.gmail_credentials_path.exists():
                        raise FileNotFoundError(
                            f"Google client secrets file not found at {self.settings.gmail_credentials_path}. "
                            f"Please make sure gog credentials exist."
                        )
                    import json
                    with open(self.settings.gmail_credentials_path, 'r') as f:
                        raw_secrets = json.load(f)
                    
                    if "installed" in raw_secrets or "web" in raw_secrets:
                        flow = InstalledAppFlow.from_client_secrets_file(
                            str(self.settings.gmail_credentials_path), SCOPES
                        )
                    else:
                        client_config = {
                            "installed": {
                                "client_id": raw_secrets.get("client_id"),
                                "client_secret": raw_secrets.get("client_secret"),
                                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                "token_uri": "https://oauth2.googleapis.com/token",
                                "redirect_uris": ["http://localhost"]
                            }
                        }
                        flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
                    import sys
                    if self.settings.headless_mode:
                        flow.redirect_uri = "http://localhost"
                        auth_url, _ = flow.authorization_url(access_type='offline', prompt='select_account')
                        
                        sys.stderr.write(f"\n[HEADLESS GMAIL OAUTH REQUIRED]:\n1. Open this URL in your desktop browser:\n👉 {auth_url}\n\n")
                        sys.stderr.write("2. Grant permissions and copy the FULL generated landing URL from your browser's address bar (starts with http://localhost...).\n")
                        sys.stderr.write("3. Paste the full redirect URL below:\n\n")
                        sys.stderr.write("Paste FULL Redirect URL here: ")
                        sys.stderr.flush()
                        
                        redirect_response = input()
                        flow.fetch_token(authorization_response=redirect_response.strip())
                        self.creds = flow.credentials
                    else:
                        def stderr_prompt_handler(url: str) -> None:
                            sys.stderr.write(f"\n[GMAIL OAUTH REQUIRED]: Please visit this URL to authorize access:\n👉 {url}\n\n")
                            sys.stderr.flush()
                        
                        self.creds = flow.run_local_server(
                            port=0, 
                            prompt='select_account', 
                            authorization_prompt_handler=stderr_prompt_handler
                        )
                
                # Save the credentials for the next run
                with open(self.settings.gmail_token_path, 'w') as token_file:
                    token_file.write(self.creds.to_json())
                logger.info("Gmail token persisted successfully to %s", self.settings.gmail_token_path)

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
        if not self.service:
            logger.error("Gmail service client is not initialized.")
            return []

        try:
            if days is not None and days > 0:
                query = f"{query} newer_than:{days}d"

            logger.info("Listing Gmail messages with query: '%s'", query)
            messages: List[Dict[str, Any]] = []
            page_token = None
            while True:
                list_params = {'userId': 'me', 'q': query}
                if page_token:
                    list_params['pageToken'] = page_token
                if max_results is not None:
                    list_params['maxResults'] = min(max_results - len(messages), 500)

                response = self.service.users().messages().list(**list_params).execute()
                messages.extend(response.get('messages', []))

                page_token = response.get('nextPageToken')
                if not page_token or (max_results is not None and len(messages) >= max_results):
                    break

            if not messages:
                logger.info("No new unread Gmail messages found matching query.")
                return []

            if max_results is not None:
                messages = messages[:max_results]

            logger.info("Found %d unread messages. Fetching metadata using HTTP batching...", len(messages))
            return self._fetch_metadata_batch(messages)
        except Exception as e:
            logger.error("Failed to list or fetch Gmail messages: %s", e, exc_info=True)
            return []

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

