import os
import logging
from typing import List, Dict, Any, Optional, Tuple
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from config import settings

logger = logging.getLogger("email_triage.gmail")

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

class GmailClient:
    def __init__(self, settings_instance: Optional[Any] = None) -> None:
        self.settings = settings_instance if settings_instance else settings
        self.creds: Optional[Credentials] = None
        self.service = None
        self._authenticate()

    def _authenticate(self) -> None:
        """Handles OAuth 2.0 authentication flow and loads/persists tokens."""
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

    def fetch_unread_messages(self, query: str = "is:unread") -> List[Dict[str, Any]]:
        """
        Fetches metadata for unread messages matching the query.
        Uses format='metadata' for token/bandwidth efficiency.
        """
        if not self.service:
            logger.error("Gmail service client is not initialized.")
            return []

        results: List[Dict[str, Any]] = []
        try:
            logger.info("Listing Gmail messages with query: '%s'", query)
            response = self.service.users().messages().list(userId='me', q=query).execute()
            messages = response.get('messages', [])

            if not messages:
                logger.info("No new unread Gmail messages found matching query.")
                return []

            logger.info("Found %d unread messages. Fetching individual metadata...", len(messages))
            for msg in messages:
                msg_id = msg['id']
                try:
                    msg_meta = self.service.users().messages().get(
                        userId='me', id=msg_id, format='metadata',
                        metadataHeaders=['Message-ID', 'From', 'Subject', 'Date']
                    ).execute()

                    headers = msg_meta.get('payload', {}).get('headers', [])
                    header_dict = {h['name']: h['value'] for h in headers}

                    message_id = header_dict.get('Message-ID', msg_id)
                    from_str = header_dict.get('From', 'Unknown Sender')
                    subject_str = header_dict.get('Subject', '(No Subject)')
                    date_str = header_dict.get('Date', '')
                    snippet_str = msg_meta.get('snippet', '')

                    results.append({
                        'id': msg_id,
                        'message_id': message_id,
                        'sender': from_str,
                        'subject': subject_str,
                        'date': date_str,
                        'snippet': snippet_str,
                        'account': self.settings.gmail_account,
                        'raw_meta': msg_meta
                    })
                except Exception as inner_e:
                    logger.error("Failed to fetch metadata for message %s: %s", msg_id, inner_e)
                    continue

            return results
        except Exception as e:
            logger.error("Failed to list or fetch Gmail messages: %s", e, exc_info=True)
            return []

    def fetch_full_body(self, msg_id: str) -> str:
        """Fetch the full body of a message if it passes triage."""
        if not self.service:
            return ""
        try:
            logger.info("Escalating: Fetching full email payload for message %s", msg_id)
            msg = self.service.users().messages().get(userId='me', id=msg_id, format='full').execute()
            
            payload = msg.get('payload', {})
            body = ""
            
            # Helper function to recursively find body parts
            def get_part_body(part: Dict[str, Any]) -> str:
                import base64
                part_body = part.get('body', {})
                data = part_body.get('data', '')
                if data:
                    return base64.urlsafe_b64decode(data.encode('utf-8')).decode('utf-8', errors='ignore')
                return ""

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
        except Exception as e:
            logger.error("Failed to fetch full body for message %s: %s", msg_id, e)
            return ""
