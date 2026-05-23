import logging
from typing import List, Dict, Any, Optional, Tuple
from imap_tools import MailBox, AND
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
