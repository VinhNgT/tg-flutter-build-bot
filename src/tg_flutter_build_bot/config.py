"""Flat environment-based configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path("data")
OAUTH_TOKEN_PATH = DATA_DIR / "oauth.json"


@dataclass(frozen=True)
class Config:
    """Bot configuration — loaded entirely from environment variables."""

    # Telegram
    telegram_token: str
    allowed_chat_ids: list[int]

    # Jenkins
    jenkins_url: str
    jenkins_user: str
    jenkins_api_token: str
    jenkins_job_name: str

    # Google Drive OAuth
    google_client_id: str
    google_client_secret: str

    # Bot webhook (Jenkins calls this)
    bot_callback_host: str  # e.g. "http://192.168.1.50:9090"
    bot_webhook_port: int = 9090

    # Optional
    gitlab_pat: str = ""
    drive_folder_name: str = ""

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
            allowed_chat_ids=[
                int(x.strip())
                for x in os.environ["ALLOWED_CHAT_IDS"].split(",")
                if x.strip()
            ],
            jenkins_url=os.environ["JENKINS_URL"],
            jenkins_user=os.environ["JENKINS_USER"],
            jenkins_api_token=os.environ["JENKINS_API_TOKEN"],
            jenkins_job_name=os.environ.get(
                "JENKINS_JOB_NAME", "flutter-build"
            ),
            google_client_id=os.environ["GOOGLE_CLIENT_ID"],
            google_client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            bot_callback_host=os.environ["BOT_CALLBACK_HOST"],
            bot_webhook_port=int(
                os.environ.get("BOT_WEBHOOK_PORT", "9090")
            ),
            gitlab_pat=os.environ.get("GITLAB_PAT", ""),
            drive_folder_name=os.environ.get("DRIVE_FOLDER_NAME", ""),
        )

    @property
    def bot_callback_url(self) -> str:
        """Full webhook URL that Jenkins calls on build completion."""
        return (
            f"{self.bot_callback_host.rstrip('/')}/webhook/build-complete"
        )
