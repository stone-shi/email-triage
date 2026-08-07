import logging
from typing import List, Dict, Any, Optional, Tuple
from imap_tools import MailBox, AND, H
from config import settings
from mail_auth import MailAuth, PasswordMailAuth, PasswordSmtpAuth, SmtpAuth

logger = logging.getLogger("email_triage.imap")

class IMAPClient:
    def __init__(
        self,
        settings_instance: Optional[Any] = None,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        login: Optional[str] = None,
        mail_auth: Optional[MailAuth] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_login: Optional[str] = None,
        smtp_auth: Optional[SmtpAuth] = None,
    ) -> None:
        self.settings = settings_instance if settings_instance else settings
        # Bind to explicit overrides when given (multi-account path), else the
        # flat config keys (unchanged default behavior).
        self.host = host or self.settings.imap_host
        self.port = port or self.settings.imap_port
        self.login_user = login or self.settings.imap_login
        self.password = self.settings.imap_password  # kept for readability/back-compat; mail_auth is authoritative
        self.mail_auth: MailAuth = mail_auth or PasswordMailAuth(self.settings.imap_password)

        self.smtp_host = smtp_host or self.settings.smtp_host
        self.smtp_port = smtp_port or self.settings.smtp_port
        self.smtp_login_user = smtp_login or self.settings.active_smtp_login
        self.smtp_auth: SmtpAuth = smtp_auth or PasswordSmtpAuth(self.settings.active_smtp_password)

    def _mailbox(self) -> MailBox:
        """Every read/write IMAP call site connects through here instead of
        calling MailBox(...).login(...) directly, so password-based and
        XOAUTH2-based (Zoho OAuth) accounts share one code path."""
        box = MailBox(self.host, port=self.port)
        return self.mail_auth.attach(box, self.login_user)

    def fetch_unread_headers(
        self,
        max_results: Optional[int] = None,
        days: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Connects to IMAP server and fetches headers only for unseen emails.
        Applies date filtering and limit constraints on the server side to prevent timeouts.
        """
        results: List[Dict[str, Any]] = []
        try:
            logger.info("Connecting to IMAP server %s:%d...", self.host, self.port)
            with self._mailbox() as mailbox:
                logger.info("Successfully logged into IMAP account. Scanning UNSEEN messages...")
                
                from datetime import date, timedelta
                if days is not None and days > 0:
                    cutoff_date = date.today() - timedelta(days=days)
                    criteria = AND(seen=False, date_gte=cutoff_date)
                else:
                    criteria = AND(seen=False)

                messages = mailbox.fetch(
                    criteria,
                    headers_only=True,
                    mark_seen=False,
                    limit=max_results
                )
                
                for msg in messages:
                    message_id = msg.headers.get('message-id', [f"imap_{msg.uid}"])[0]
                    from_str = msg.from_
                    subject_str = msg.subject
                    date_str = str(msg.date)
                    snippet_str = msg.desc if hasattr(msg, 'desc') and msg.desc else f"Subject: {subject_str}"

                    results.append({
                        'id': msg.uid,
                        'message_id': message_id,
                        'sender': from_str,
                        'subject': subject_str,
                        'date': date_str,
                        'snippet': snippet_str,
                        'account': self.login_user
                    })

            logger.info("Fetched %d unread emails from IMAP server.", len(results))
            return results
        except Exception as e:
            logger.error("Failed to fetch unread emails from IMAP: %s", e, exc_info=True)
            return []

    def fetch_all_headers(self, max_results: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Connects to IMAP server and fetches headers for EVERY message in the mailbox (not scoped
        to unread), for a one-time full-archive download. Scoped to the default/selected folder
        (INBOX), same as fetch_unread_headers -- no multi-folder support yet.
        """
        results: List[Dict[str, Any]] = []
        try:
            logger.info("Connecting to IMAP server %s:%d for full mailbox listing...", self.host, self.port)
            with self._mailbox() as mailbox:
                logger.info("Successfully logged into IMAP account. Scanning ALL messages...")
                messages = mailbox.fetch(AND(all=True), headers_only=True, mark_seen=False, limit=max_results)

                for msg in messages:
                    message_id = msg.headers.get('message-id', [f"imap_{msg.uid}"])[0]
                    from_str = msg.from_
                    subject_str = msg.subject
                    date_str = str(msg.date)
                    snippet_str = msg.desc if hasattr(msg, 'desc') and msg.desc else f"Subject: {subject_str}"

                    results.append({
                        'id': msg.uid,
                        'message_id': message_id,
                        'sender': from_str,
                        'subject': subject_str,
                        'date': date_str,
                        'snippet': snippet_str,
                        'account': self.login_user
                    })

            logger.info("Fetched %d total messages from IMAP server.", len(results))
            return results
        except Exception as e:
            logger.error("Failed to fetch all messages from IMAP: %s", e, exc_info=True)
            return []

    def fetch_full_body(self, uid: str) -> str:
        """Fetch full body if email passes triage."""
        try:
            logger.info("Escalating: Fetching full IMAP email payload for UID %s", uid)
            with self._mailbox() as mailbox:
                for msg in mailbox.fetch(AND(uid=uid), mark_seen=False):
                    if msg.text:
                        return msg.text
                    elif msg.html:
                        return msg.html
            return ""
        except Exception as e:
            logger.error("Failed to fetch full body for IMAP UID %s: %s", uid, e)
            return ""

    def fetch_full_bodies_batch(self, uids: List[str], chunk_size: int = 100) -> Dict[str, str]:
        """
        Fetches full body content for multiple UIDs over a single IMAP connection using
        multi-UID FETCH commands (chunked to avoid overly long UID lists in one command),
        instead of opening a new connection per message. UIDs that can't be resolved are
        simply omitted from the result.
        """
        if not uids:
            return {}

        results: Dict[str, str] = {}
        try:
            logger.info(
                "Connecting to IMAP server %s:%d to batch-fetch %d full bodies...",
                self.host, self.port, len(uids),
            )
            with self._mailbox() as mailbox:
                for i in range(0, len(uids), chunk_size):
                    chunk = uids[i:i + chunk_size]
                    try:
                        for msg in mailbox.fetch(AND(uid=chunk), mark_seen=False):
                            results[msg.uid] = msg.text or msg.html or ""
                    except Exception as chunk_err:
                        logger.error("Failed to batch-fetch IMAP bodies for a chunk: %s", chunk_err)
        except Exception as e:
            logger.error("Failed to connect for IMAP batch body fetch: %s", e, exc_info=True)

        return results

    def mark_as_read(self, uids: List[str]) -> bool:
        """Mark a list of IMAP message UIDs as read by setting the \\Seen flag."""
        if not uids:
            return False
        try:
            logger.info("Marking %d IMAP messages as read...", len(uids))
            with self._mailbox() as mailbox:
                mailbox.flag(uids, '\\Seen', True)
            return True
        except Exception as e:
            logger.error("Failed to mark IMAP messages as read: %s", e)
            return False

    def search_messages(self, query: str) -> List[Dict[str, Any]]:
        """
        Connects to IMAP server and searches for emails matching the query.
        """
        results: List[Dict[str, Any]] = []
        try:
            logger.info("Connecting to IMAP server %s:%d for search...", self.host, self.port)
            with self._mailbox() as mailbox:
                logger.info("Searching IMAP messages for query: '%s'", query)
                messages = mailbox.fetch(AND(text=query), headers_only=True, mark_seen=False)
                
                for msg in messages:
                    message_id = msg.headers.get('message-id', [f"imap_{msg.uid}"])[0]
                    from_str = msg.from_
                    subject_str = msg.subject
                    date_str = str(msg.date)
                    snippet_str = msg.desc if hasattr(msg, 'desc') and msg.desc else f"Subject: {subject_str}"

                    results.append({
                        'id': msg.uid,
                        'message_id': message_id,
                        'sender': from_str,
                        'subject': subject_str,
                        'date': date_str,
                        'snippet': snippet_str,
                        'account': self.login_user
                    })

            logger.info("Found %d matching emails from IMAP server.", len(results))
            return results
        except Exception as e:
            logger.error("Failed to search emails from IMAP: %s", e, exc_info=True)
            return []

    def _find_message(self, message_id_or_uid: str) -> Dict[str, Any]:
        """
        Finds a message on the IMAP server by UID or RFC Message-ID.
        Returns basic header details.
        """
        try:
            logger.info("Connecting to IMAP server %s:%d to find message %s...", self.host, self.port, message_id_or_uid)
            with self._mailbox() as mailbox:
                # 1. Try treating message_id_or_uid as UID first
                if message_id_or_uid.isdigit():
                    messages = list(mailbox.fetch(AND(uid=message_id_or_uid), headers_only=True, mark_seen=False))
                    if messages:
                        msg = messages[0]
                        return {
                            "uid": msg.uid,
                            "message_id": msg.headers.get('message-id', [f"imap_{msg.uid}"])[0],
                            "from": msg.from_,
                            "reply-to": msg.headers.get('reply-to', [''])[0],
                            "subject": msg.subject,
                            "thread_id": msg.headers.get('thread-id', [''])[0],
                            "references": msg.headers.get('references', [''])[0]
                        }
                
                # 2. Query by RFC Message-ID
                clean_id = message_id_or_uid
                if not clean_id.startswith("<"):
                    clean_id = f"<{clean_id}>"
                
                messages = list(mailbox.fetch(AND(header=H("Message-ID", clean_id)), headers_only=True, mark_seen=False))
                if not messages:
                    messages = list(mailbox.fetch(AND(header=H("Message-ID", message_id_or_uid)), headers_only=True, mark_seen=False))
                
                if messages:
                    msg = messages[0]
                    return {
                        "uid": msg.uid,
                        "message_id": msg.headers.get('message-id', [f"imap_{msg.uid}"])[0],
                        "from": msg.from_,
                        "reply-to": msg.headers.get('reply-to', [''])[0],
                        "subject": msg.subject,
                        "thread_id": msg.headers.get('thread-id', [''])[0],
                        "references": msg.headers.get('references', [''])[0]
                    }
                
                raise ValueError(f"Message not found on IMAP server with UID or Message-ID: {message_id_or_uid}")
        except Exception as e:
            logger.error("Failed to find message in IMAP: %s", e, exc_info=True)
            raise

    def fetch_full_email(self, message_id_or_uid: str) -> Dict[str, Any]:
        """
        Fetches full headers + body for a single message, accepting either the IMAP UID or the
        RFC 2822 Message-ID (resolved via _find_message).
        """
        parent_msg = self._find_message(message_id_or_uid)
        return {
            'id': parent_msg['uid'],
            'message_id': parent_msg.get('message_id', ''),
            'sender': parent_msg.get('from', ''),
            'subject': parent_msg.get('subject', ''),
            'date': '',
            'body': self.fetch_full_body(parent_msg['uid']),
            'account': self.login_user,
        }

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Creates a draft on the IMAP server by appending to the Drafts folder.
        """
        try:
            from email.message import EmailMessage
            mime_msg = EmailMessage()
            mime_msg["To"] = to
            mime_msg["Subject"] = subject
            mime_msg["From"] = self.login_user
            mime_msg.set_content(body)

            if in_reply_to:
                mime_msg["In-Reply-To"] = in_reply_to
            if references:
                mime_msg["References"] = references

            logger.info("Connecting to IMAP server %s:%d to create draft...", self.host, self.port)
            with self._mailbox() as mailbox:
                folders = [f.name for f in mailbox.folder.list()]
                drafts_folder = 'Drafts'
                for f in folders:
                    if f.lower() in ('drafts', 'draft', '草稿箱', '草稿'):
                        drafts_folder = f
                        break
                
                logger.info("Appending draft to folder '%s'", drafts_folder)
                res = mailbox.append(
                    message=mime_msg.as_bytes(),
                    folder=drafts_folder,
                    flag_set='\\Draft'
                )
                return {
                    "status": "success",
                    "folder": drafts_folder,
                    "append_result": str(res)
                }
        except Exception as e:
            logger.error("Failed to create IMAP draft: %s", e, exc_info=True)
            raise

    def create_reply_draft(self, message_id: str, body: str) -> Dict[str, Any]:
        """
        Creates a draft reply to an existing message on the IMAP server.
        """
        parent_msg = self._find_message(message_id)
        
        to = parent_msg.get('reply-to') or parent_msg.get('from')
        if not to:
            raise ValueError(f"Could not identify the sender of message {message_id}")
            
        subject = parent_msg.get('subject', '')
        if subject and not subject.lower().startswith('re:'):
            subject = f"Re: {subject}"
        elif not subject:
            subject = "Re: (No Subject)"
            
        parent_rfc_msg_id = parent_msg.get('message_id')
        references = parent_msg.get('references', '')
        
        if parent_rfc_msg_id:
            if references:
                references = f"{references} {parent_rfc_msg_id}"
            else:
                references = parent_rfc_msg_id
                
        return self.create_draft(
            to=to,
            subject=subject,
            body=body,
            in_reply_to=parent_rfc_msg_id,
            references=references
        )

    def send_reply(self, message_id: str, body: str) -> Dict[str, Any]:
        """
        Sends a reply to an existing message via SMTP and saves a copy to the Sent folder.
        """
        import smtplib
        from email.message import EmailMessage

        parent_msg = self._find_message(message_id)
        
        to = parent_msg.get('reply-to') or parent_msg.get('from')
        if not to:
            raise ValueError(f"Could not identify the sender of message {message_id}")
            
        subject = parent_msg.get('subject', '')
        if subject and not subject.lower().startswith('re:'):
            subject = f"Re: {subject}"
        elif not subject:
            subject = "Re: (No Subject)"
            
        parent_rfc_msg_id = parent_msg.get('message_id')
        references = parent_msg.get('references', '')
        
        if parent_rfc_msg_id:
            if references:
                references = f"{references} {parent_rfc_msg_id}"
            else:
                references = parent_rfc_msg_id

        mime_msg = EmailMessage()
        mime_msg["To"] = to
        mime_msg["Subject"] = subject
        mime_msg["From"] = self.login_user
        mime_msg.set_content(body)

        if parent_rfc_msg_id:
            mime_msg["In-Reply-To"] = parent_rfc_msg_id
        if references:
            mime_msg["References"] = references

        # 1. Send via SMTP
        host = self.smtp_host
        port = self.smtp_port
        login = self.smtp_login_user

        logger.info("Connecting to SMTP server %s:%d to send reply...", host, port)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30.0) as server:
                self.smtp_auth.authenticate(server, login)
                server.send_message(mime_msg)
        else:
            with smtplib.SMTP(host, port, timeout=30.0) as server:
                server.ehlo()
                try:
                    server.starttls()
                    server.ehlo()
                except Exception as tls_err:
                    logger.warning("STARTTLS failed: %s", tls_err)
                self.smtp_auth.authenticate(server, login)
                server.send_message(mime_msg)
        
        logger.info("Successfully sent reply via SMTP.")

        # 2. Append copy to Sent folder
        try:
            logger.info("Connecting to IMAP server %s:%d to save copy of sent mail...", self.host, self.port)
            with self._mailbox() as mailbox:
                folders = [f.name for f in mailbox.folder.list()]
                sent_folder = 'Sent'
                for f in folders:
                    if f.lower() in ('sent', 'sent messages', 'sent items', '已发送'):
                        sent_folder = f
                        break
                
                logger.info("Appending sent message copy to folder '%s'", sent_folder)
                mailbox.append(
                    message=mime_msg.as_bytes(),
                    folder=sent_folder,
                    flag_set='\\Seen'
                )
        except Exception as append_err:
            logger.error("Failed to append sent copy to IMAP folder: %s", append_err)
            
        return {
            "status": "success",
            "sent_to": to,
            "subject": subject
        }
