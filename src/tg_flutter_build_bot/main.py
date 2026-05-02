"""Entry point — runs Telegram polling + webhook server concurrently."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .bot.context import BotContext
from .bot.handlers import (
    AWAITING_CODE,
    build_handler,
    connect_drive_cancel,
    connect_drive_code,
    connect_drive_start,
    recent_handler,
    start_handler,
    status_handler,
)
from .config import Config
from .drive.uploader import DriveUploader
from .jenkins.client import JenkinsClient
from .jenkins.webhook import create_webhook_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def run() -> None:
    """Start both the Telegram bot and webhook server."""
    config = Config.from_env()

    # Jenkins client
    jenkins = JenkinsClient(
        url=config.jenkins_url,
        user=config.jenkins_user,
        api_token=config.jenkins_api_token,
        job_name=config.jenkins_job_name,
    )

    # Drive uploader
    drive = DriveUploader(
        client_id=config.google_client_id,
        client_secret=config.google_client_secret,
    )

    # Telegram application
    app = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .build()
    )

    # Bot context (shared state)
    bot_context = BotContext(
        config=config,
        jenkins=jenkins,
        drive=drive,
        bot=app.bot,
    )
    app.bot_data["bot_context"] = bot_context

    # Register handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("build", build_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("recent", recent_handler))

    # /connect_drive conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("connect_drive", connect_drive_start)
        ],
        states={
            AWAITING_CODE: [
                CommandHandler("cancel", connect_drive_cancel),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    connect_drive_code,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", connect_drive_cancel)],
    )
    app.add_handler(conv_handler)

    # Webhook server (for Jenkins callbacks)
    webhook_app = create_webhook_app(bot_context)
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.bot_webhook_port)
    await site.start()
    logger.info(
        "Webhook server listening on port %d", config.bot_webhook_port
    )

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _signal_handler(sig: int, _frame) -> None:
        logger.info("Received signal %d, shutting down...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Start Telegram polling
    await app.initialize()
    await app.start()
    assert app.updater  # Always set when built via ApplicationBuilder
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot started")

    # Block until shutdown signal
    await stop_event.wait()

    # Teardown
    logger.info("Stopping Telegram bot...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()

    logger.info("Stopping webhook server...")
    await runner.cleanup()

    logger.info("Shutdown complete.")


def cli() -> None:
    """CLI entry point."""
    asyncio.run(run())
