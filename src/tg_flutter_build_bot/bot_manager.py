"""Telegram bot lifecycle manager — start, stop, restart without process restart."""

from __future__ import annotations

import asyncio
import logging

from telegram.ext import ApplicationBuilder, CommandHandler

from .bot.filters import ChatWhitelistFilter
from .bot.handlers import (
    build_handler,
    recent_handler,
    start_handler,
    status_handler,
)
from .builder.service import BuilderService
from .drive.uploader import DriveUploader
from .store import Store

logger = logging.getLogger(__name__)


class BotManager:
    """Manages the Telegram bot start/stop/restart lifecycle.

    The web server and other services are unaffected — only the
    Telegram polling bot is controlled here.
    """

    def __init__(
        self,
        store: Store,
        builder_service: BuilderService,
        drive_uploader: DriveUploader,
    ) -> None:
        self._store = store
        self._builder_service = builder_service
        self._drive_uploader = drive_uploader
        self._bot_app = None
        self._lock = asyncio.Lock()

    @property
    def bot_running(self) -> bool:
        """Whether the Telegram bot is currently polling."""
        return self._bot_app is not None

    async def start_bot(self) -> None:
        """Start the bot with current config.

        No-op if already running or if the token is not configured.
        """
        async with self._lock:
            if self._bot_app is not None:
                logger.info("Bot is already running")
                return

            config = self._store.get_effective_config()
            if not config.telegram_token:
                logger.warning("Cannot start bot: TELEGRAM_BOT_TOKEN not configured")
                return

            app = self._create_bot(config.telegram_token)
            await app.initialize()
            await app.start()
            await app.updater.start_polling()

            self._bot_app = app
            logger.info("Telegram bot started")

    async def stop_bot(self) -> None:
        """Stop the bot gracefully. No-op if not running."""
        async with self._lock:
            if self._bot_app is None:
                return

            await self._bot_app.updater.stop()
            await self._bot_app.stop()
            await self._bot_app.shutdown()
            self._bot_app = None
            logger.info("Telegram bot stopped")

    async def restart_bot(self) -> None:
        """Stop then start the bot with the latest config."""
        await self.stop_bot()
        await self.start_bot()

    def _create_bot(self, token: str):
        """Build and configure the Telegram Application with all handlers."""
        app = ApplicationBuilder().token(token).build()
        whitelist = ChatWhitelistFilter(self._store)

        store = self._store
        build_service = self._builder_service
        drive_uploader = self._drive_uploader

        # /start
        app.add_handler(
            CommandHandler("start", start_handler, filters=whitelist)
        )

        # /build [ref]
        async def _build(update, context):
            await build_handler(
                update, context, store, build_service, drive_uploader
            )

        app.add_handler(CommandHandler("build", _build, filters=whitelist))

        # /status
        async def _status(update, context):
            await status_handler(update, context, store, build_service)

        app.add_handler(CommandHandler("status", _status, filters=whitelist))

        # /recent
        async def _recent(update, context):
            await recent_handler(update, context, store)

        app.add_handler(CommandHandler("recent", _recent, filters=whitelist))

        return app
