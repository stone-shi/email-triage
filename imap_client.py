import logging
from typing import List, Dict, Any, Optional, Tuple
from imap_tools import MailBox, AND
from config import settings

logger = logging.getLogger("email_triage.imap")

class IMAPClient:
    def __init__(self) -> None:
        # Bind directly to flat root-level config keys
        self.host = settings.imap_host
        self.port = settings.imap_port
        self.login_user = settings.imap_login
        self.password = settings.imap_password

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
