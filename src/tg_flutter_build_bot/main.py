"""Entry point — starts both the Telegram bot and FastAPI web server."""

from __future__ import annotations

import asyncio
import logging
import sys
from functools import partial

import uvicorn
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, CommandHandler

from .bot.filters import ChatWhitelistFilter
from .bot.handlers import (
    build_handler,
    builds_handler,
    start_handler,
    status_handler,
)
from .build.service import BuildService
from .drive.uploader import DriveUploader
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def create_bot(
    token: str,
    store: Store,
    build_service: BuildService,
    drive_uploader: DriveUploader,
):
    """Create and configure the Telegram bot application."""
    app = ApplicationBuilder().token(token).build()
    whitelist = ChatWhitelistFilter(store)

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

    # /builds
    async def _builds(update, context):
        await builds_handler(update, context, store)

    app.add_handler(CommandHandler("builds", _builds, filters=whitelist))

    return app


async def main() -> None:
    """Start both the Telegram bot and FastAPI web server concurrently."""
    # Load .env file (does NOT override existing env vars)
    load_dotenv()

    # Initialize shared state
    store = Store()
    config = store.get_effective_config()

    if not config.telegram_token:
        logger.error(
            "TELEGRAM_BOT_TOKEN not set. Configure via .env, env var, or Web UI."
        )
        sys.exit(1)

    # Initialize services
    build_service = BuildService()
    drive_uploader = DriveUploader()

    # Create web app
    from .web.app import create_app

    web_app = create_app(store, drive_uploader)

    # Configure uvicorn
    uvi_config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=config.web_port,
        log_level="info",
    )
    server = uvicorn.Server(uvi_config)

    # Create Telegram bot
    bot_app = create_bot(
        config.telegram_token, store, build_service, drive_uploader
    )

    logger.info("Starting Flutter Build Bot...")
    logger.info("Web UI: http://localhost:%d", config.web_port)
    logger.info("Repo: %s", config.repo_url or "(not configured)")

    # Run both concurrently
    async with bot_app:
        await bot_app.start()
        await bot_app.updater.start_polling()

        try:
            await server.serve()
        finally:
            await bot_app.updater.stop()
            await bot_app.stop()


def cli() -> None:
    """CLI entry point for the bot."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    cli()
