"""Google Drive integration — Desktop OAuth2 flow and file upload/management."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build as build_service
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_PATH = Path("data/oauth.json")


class DriveUploader:
    """Handles Google OAuth2 (Desktop type) and Drive file operations.

    OAuth flow:
    1. get_auth_url() — generates consent URL for the user
    2. exchange_code() — exchanges the pasted auth code for tokens
    3. Tokens are persisted to data/oauth.json and auto-refreshed
    """

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._folder_id_cache: dict[str, str] = {}
        self._pending_flow: InstalledAppFlow | None = None

    def _client_config(self) -> dict:
        """Build the Desktop (installed) OAuth client config."""
        return {
            "installed": {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    # ------------------------------------------------------------------
    # OAuth flow (one-time setup via Telegram /connect_drive)
    # ------------------------------------------------------------------

    def get_auth_url(self) -> str:
        """Generate the Google OAuth consent URL for Desktop flow."""
        flow = InstalledAppFlow.from_client_config(
            self._client_config(),
            scopes=SCOPES,
        )
        flow.redirect_uri = "http://localhost"
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
        # Keep the flow alive for exchange_code()
        self._pending_flow = flow
        return auth_url

    def exchange_code(self, code: str) -> None:
        """Exchange an authorization code for OAuth tokens and save them."""
        flow = self._pending_flow
        if flow is None:
            raise RuntimeError(
                "No pending OAuth flow — run /connect_drive first."
            )
        self._pending_flow = None

        flow.fetch_token(code=code)
        creds = flow.credentials

        # Save tokens to disk
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(
            json.dumps(
                {
                    "token": creds.token,
                    "refresh_token": creds.refresh_token,
                    "token_uri": creds.token_uri,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes": list(creds.scopes or []),
                }
            )
        )
        logger.info("OAuth tokens saved to %s", TOKEN_PATH)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def load_tokens(self) -> Credentials | None:
        """Load saved OAuth tokens from disk, refreshing if needed."""
        if not TOKEN_PATH.exists():
            return None

        data = json.loads(TOKEN_PATH.read_text())
        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get(
                "token_uri", "https://oauth2.googleapis.com/token"
            ),
            client_id=data.get("client_id", self._client_id),
            client_secret=data.get("client_secret", self._client_secret),
            scopes=data.get("scopes"),
        )

        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
            # Persist the refreshed access token
            data["token"] = creds.token
            TOKEN_PATH.write_text(json.dumps(data))

        return creds if creds.valid else None

    def is_connected(self) -> bool:
        """Check if Google Drive is connected (valid tokens exist)."""
        return self.load_tokens() is not None

    # ------------------------------------------------------------------
    # Drive file operations
    # ------------------------------------------------------------------

    def _get_drive_service(self, creds: Credentials):
        """Build an authenticated Drive API service."""
        return build_service("drive", "v3", credentials=creds)

    async def ensure_folder(
        self, creds: Credentials, folder_name: str
    ) -> str:
        """Find or create a Drive folder by name. Returns folder ID."""
        if folder_name in self._folder_id_cache:
            return self._folder_id_cache[folder_name]

        service = self._get_drive_service(creds)

        query = (
            f"name = '{folder_name}' and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"trashed = false"
        )
        results = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id, name)")
            .execute()
        )
        files = results.get("files", [])

        if files:
            folder_id = files[0]["id"]
            logger.info(
                "Found existing Drive folder '%s' (ID: %s)",
                folder_name,
                folder_id,
            )
        else:
            file_metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = (
                service.files()
                .create(body=file_metadata, fields="id")
                .execute()
            )
            folder_id = folder["id"]
            logger.info(
                "Created Drive folder '%s' (ID: %s)",
                folder_name,
                folder_id,
            )

        self._folder_id_cache[folder_name] = folder_id
        return folder_id

    async def upload_file(
        self,
        file_path: str,
        filename: str,
        creds: Credentials,
        folder_id: str,
    ) -> tuple[str, str]:
        """Upload a file to Google Drive.

        Returns (file_id, web_view_link).
        """
        service = self._get_drive_service(creds)

        file_metadata = {"name": filename, "parents": [folder_id]}
        media = MediaFileUpload(
            file_path,
            mimetype="application/vnd.android.package-archive",
            resumable=True,
        )

        logger.info("Uploading %s to Drive folder %s", filename, folder_id)

        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, webViewLink",
            )
            .execute()
        )

        file_id = file["id"]

        # Make file viewable by anyone with the link
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()

        # Re-fetch to get the updated link
        file = (
            service.files()
            .get(fileId=file_id, fields="webViewLink")
            .execute()
        )
        web_link = file.get("webViewLink", "")

        logger.info("Uploaded: %s -> %s", filename, web_link)
        return file_id, web_link

    async def delete_file(
        self, file_id: str, creds: Credentials
    ) -> None:
        """Delete a file from Google Drive."""
        try:
            service = self._get_drive_service(creds)
            service.files().delete(fileId=file_id).execute()
            logger.info("Deleted Drive file: %s", file_id)
        except Exception as e:
            logger.warning(
                "Failed to delete Drive file %s: %s", file_id, e
            )
