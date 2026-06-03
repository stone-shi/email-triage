import logging
from typing import List, Dict, Any, Optional, Tuple
from imap_tools import MailBox, AND, H
from config import settings

logger = logging.getLogger("email_triage.imap")

class IMAPClient:
    def __init__(self, settings_instance: Optional[Any] = None) -> None:
        self.settings = settings_instance if settings_instance else settings
        # Bind directly to flat config keys
        self.host = self.settings.imap_host
        self.port = self.settings.imap_port
        self.login_user = self.settings.imap_login
        self.password = self.settings.imap_password

    def fetch_unread_headers(self) -> List[Dict[str, Any]]:
        """
        Connects to IMAP server and fetches headers only for unseen emails.
        """
        results: List[Dict[str, Any]] = []
        try:
            logger.info("Connecting to IMAP server %s:%d...", self.host, self.port)
            with MailBox(self.host, port=self.port).login(self.login_user, self.password) as mailbox:
                logger.info("Successfully logged into IMAP account. Scanning UNSEEN messages...")
                
                messages = mailbox.fetch(AND(seen=False), headers_only=True, mark_seen=False)
                
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

    def fetch_full_body(self, uid: str) -> str:
        """Fetch full body if email passes triage."""
        try:
            logger.info("Escalating: Fetching full IMAP email payload for UID %s", uid)
            with MailBox(self.host, port=self.port).login(self.login_user, self.password) as mailbox:
                for msg in mailbox.fetch(AND(uid=uid), mark_seen=False):
                    if msg.text:
                        return msg.text
                    elif msg.html:
                        return msg.html
            return ""
        except Exception as e:
            logger.error("Failed to fetch full body for IMAP UID %s: %s", uid, e)
            return ""

    def mark_as_read(self, uids: List[str]) -> bool:
        """Mark a list of IMAP message UIDs as read by setting the \\Seen flag."""
        if not uids:
            return False
        try:
            logger.info("Marking %d IMAP messages as read...", len(uids))
            with MailBox(self.host, port=self.port).login(self.login_user, self.password) as mailbox:
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
            with MailBox(self.host, port=self.port).login(self.login_user, self.password) as mailbox:
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
            with MailBox(self.host, port=self.port).login(self.login_user, self.password) as mailbox:
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
            with MailBox(self.host, port=self.port).login(self.login_user, self.password) as mailbox:
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
        host = self.settings.smtp_host
        port = self.settings.smtp_port
        login = self.settings.active_smtp_login
        password = self.settings.active_smtp_password
        
        logger.info("Connecting to SMTP server %s:%d to send reply...", host, port)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30.0) as server:
                server.login(login, password)
                server.send_message(mime_msg)
        else:
            with smtplib.SMTP(host, port, timeout=30.0) as server:
                server.ehlo()
                try:
                    server.starttls()
                    server.ehlo()
                except Exception as tls_err:
                    logger.warning("STARTTLS failed: %s", tls_err)
                server.login(login, password)
                server.send_message(mime_msg)
        
        logger.info("Successfully sent reply via SMTP.")

        # 2. Append copy to Sent folder
        try:
            logger.info("Connecting to IMAP server %s:%d to save copy of sent mail...", self.host, self.port)
            with MailBox(self.host, port=self.port).login(self.login_user, self.password) as mailbox:
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
