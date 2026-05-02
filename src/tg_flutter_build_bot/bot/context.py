"""Bot context — shared state between Telegram handlers and webhook."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Bot

if TYPE_CHECKING:
    from ..config import Config
    from ..drive.uploader import DriveUploader
    from ..jenkins.client import JenkinsClient

logger = logging.getLogger(__name__)

PENDING_BUILD_TTL = 3600  # 1 hour


@dataclass
class PendingBuild:
    """Tracks a build triggered via Telegram."""

    chat_id: int
    ref: str
    triggered_at: float


class BotContext:
    """Shared context between Telegram handlers and the webhook server.

    Owns:
    - Pending build tracking (queue_id → chat_id mapping)
    - Build result handling (Drive upload + Telegram notification)
    """

    def __init__(
        self,
        config: Config,
        jenkins: JenkinsClient,
        drive: DriveUploader,
        bot: Bot,
    ) -> None:
        self.config = config
        self.jenkins = jenkins
        self.drive = drive
        self.bot = bot
        self._pending: dict[int, PendingBuild] = {}

    # ------------------------------------------------------------------
    # Pending build tracking
    # ------------------------------------------------------------------

    def add_pending(
        self, queue_id: int, chat_id: int, ref: str
    ) -> None:
        """Track a Telegram-triggered build."""
        self._cleanup_expired()
        self._pending[queue_id] = PendingBuild(
            chat_id=chat_id,
            ref=ref,
            triggered_at=time.time(),
        )

    def consume_pending(self, queue_id: int | None) -> PendingBuild | None:
        """Look up and remove a pending build. Returns None if not found."""
        if queue_id is None:
            return None
        self._cleanup_expired()
        return self._pending.pop(queue_id, None)

    def _cleanup_expired(self) -> None:
        """Remove pending builds older than TTL."""
        now = time.time()
        expired = [
            qid
            for qid, p in self._pending.items()
            if now - p.triggered_at > PENDING_BUILD_TTL
        ]
        for qid in expired:
            del self._pending[qid]

    # ------------------------------------------------------------------
    # Build result handlers (called by webhook)
    # ------------------------------------------------------------------

    async def on_build_success(
        self,
        pending: PendingBuild,
        metadata: dict,
        artifact_path: str,
    ) -> None:
        """Handle successful build — upload to Drive and notify user."""
        commit_hash = metadata.get("commit_hash", "unknown")
        short_hash = commit_hash[:7]

        try:
            creds = self.drive.load_tokens()
            if not creds:
                await self.bot.send_message(
                    pending.chat_id,
                    f"✅ Build successful (`{short_hash}`) but Google Drive "
                    f"is not connected.\n"
                    f"Use /connect\\_drive to set up uploads.",
                    parse_mode="Markdown",
                )
                return

            await self.bot.send_message(
                pending.chat_id,
                "☁️ Build complete! Uploading to Google Drive...",
            )

            # Generate filename
            now = datetime.now(timezone.utc)
            folder_name = self.config.drive_folder_name or "flutter-builds"
            filename = (
                f"{folder_name}-{now.strftime('%Y%m%d-%H%M')}"
                f"-{short_hash}.apk"
            )

            folder_id = await self.drive.ensure_folder(creds, folder_name)
            file_id, drive_link = await self.drive.upload_file(
                artifact_path, filename, creds, folder_id
            )

            await self.bot.send_message(
                pending.chat_id,
                f"✅ Build successful!\n\n"
                f"📦 `{filename}`\n"
                f"🔗 [Download APK]({drive_link})",
                parse_mode="Markdown",
            )

        except Exception as e:
            logger.exception(
                "Failed to upload/notify for build %s", commit_hash
            )
            await self.bot.send_message(
                pending.chat_id,
                f"✅ Build succeeded (`{short_hash}`) but upload failed: {e}",
                parse_mode="Markdown",
            )

        finally:
            Path(artifact_path).unlink(missing_ok=True)

    async def on_build_failure(
        self, pending: PendingBuild, metadata: dict
    ) -> None:
        """Handle failed build — notify user."""
        commit_hash = metadata.get("commit_hash", "unknown")
        short_hash = commit_hash[:7]
        logs = metadata.get("logs", "No logs available")

        await self.bot.send_message(
            pending.chat_id,
            f"❌ Build failed for `{short_hash}`\n\n"
            f"```\n{logs[:500]}\n```\n\n"
            f"Check Jenkins console for full logs.",
            parse_mode="Markdown",
        )
