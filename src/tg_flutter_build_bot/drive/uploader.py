"""Google Drive integration — OAuth2 flow and file upload/management."""

from __future__ import annotations

import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build as build_service
from googleapiclient.http import MediaFileUpload

from ..config import OAuthConfig

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
REDIRECT_PATH = "/oauth/callback"


class DriveError(Exception):
    """Raised when a Drive operation fails."""


class DriveUploader:
    """Handles Google OAuth2 and Drive file operations.

    Responsibilities:
    - Generate OAuth consent URLs
    - Exchange auth codes for tokens
    - Upload files to a named folder
    - Delete files
    - Auto-refresh access tokens
    """

    def __init__(self) -> None:
        self._folder_id_cache: dict[str, str] = {}  # folder_name -> folder_id

    def get_auth_url(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> str:
        """Generate the Google OAuth consent URL."""
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return auth_url

    def exchange_code(
        self,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> OAuthConfig:
        """Exchange an authorization code for OAuth tokens."""
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        return OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=creds.refresh_token or "",
            access_token=creds.token or "",
        )

    def _get_credentials(self, oauth: OAuthConfig) -> Credentials:
        """Build google.oauth2 Credentials from our config, refreshing if needed."""
        creds = Credentials(
            token=oauth.access_token,
            refresh_token=oauth.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=oauth.client_id,
            client_secret=oauth.client_secret,
            scopes=SCOPES,
        )
        if not creds.valid:
            creds.refresh(Request())
            # Update our config with new access token
            oauth.access_token = creds.token or ""
        return creds

    def _get_drive_service(self, oauth: OAuthConfig):
        """Build an authenticated Drive API service."""
        creds = self._get_credentials(oauth)
        return build_service("drive", "v3", credentials=creds)

    async def ensure_folder(
        self, oauth: OAuthConfig, folder_name: str
    ) -> str:
        """Find or create a Drive folder by name. Returns folder ID.

        Searches for an existing folder with the given name in the root.
        Creates one if not found. Caches the result.
        """
        if folder_name in self._folder_id_cache:
            return self._folder_id_cache[folder_name]

        service = self._get_drive_service(oauth)

        # Search for existing folder
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
            # Create the folder
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
                "Created Drive folder '%s' (ID: %s)", folder_name, folder_id
            )

        self._folder_id_cache[folder_name] = folder_id
        return folder_id

    async def upload_file(
        self,
        file_path: str,
        filename: str,
        oauth: OAuthConfig,
        folder_id: str,
    ) -> tuple[str, str]:
        """Upload a file to Google Drive.

        Args:
            file_path: Local path to the file.
            filename: Name for the file on Drive.
            oauth: OAuth config with valid tokens.
            folder_id: Drive folder ID to upload into.

        Returns:
            (file_id, web_view_link) tuple.
        """
        service = self._get_drive_service(oauth)

        file_metadata = {
            "name": filename,
            "parents": [folder_id],
        }
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
        web_link = file.get("webViewLink", "")

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
        web_link = file.get("webViewLink", web_link)

        logger.info("Uploaded: %s -> %s", filename, web_link)
        return file_id, web_link

    async def delete_file(
        self, file_id: str, oauth: OAuthConfig
    ) -> None:
        """Delete a file from Google Drive."""
        try:
            service = self._get_drive_service(oauth)
            service.files().delete(fileId=file_id).execute()
            logger.info("Deleted Drive file: %s", file_id)
        except Exception as e:
            logger.warning("Failed to delete Drive file %s: %s", file_id, e)

    async def check_file_exists(
        self, file_id: str, oauth: OAuthConfig
    ) -> bool:
        """Check if a file still exists on Drive."""
        try:
            service = self._get_drive_service(oauth)
            service.files().get(fileId=file_id, fields="id").execute()
            return True
        except Exception:
            return False
