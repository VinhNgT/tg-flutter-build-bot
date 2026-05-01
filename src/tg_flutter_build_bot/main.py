"""Entry point — starts both the Telegram bot and FastAPI web server."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from dotenv import load_dotenv

from .bot_manager import BotManager
from .builder.service import BuilderService
from .drive.uploader import DriveUploader
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Start both the Telegram bot and FastAPI web server concurrently."""
    # Load .env file (does NOT override existing env vars)
    load_dotenv()

    # Initialize shared state
    store = Store()
    config = store.get_effective_config()

    # Initialize services
    builder_service = BuilderService()
    drive_uploader = DriveUploader()
    bot_manager = BotManager(store, builder_service, drive_uploader)

    # Create web app (always starts regardless of bot config)
    from .web.app import create_app

    web_app = create_app(store, drive_uploader, bot_manager)

    # Configure uvicorn
    uvi_config = uvicorn.Config(
        web_app,
        host="0.0.0.0",
        port=config.web_port,
        log_level="info",
    )
    server = uvicorn.Server(uvi_config)

    # Attempt to start the Telegram bot (non-fatal if token missing)
    await bot_manager.start_bot()

    if not bot_manager.bot_running:
        logger.warning(
            "Telegram bot not started — configure via http://localhost:%d/config",
            config.web_port,
        )

    logger.info("Starting Flutter Build Bot...")
    logger.info("Web UI: http://localhost:%d", config.web_port)
    logger.info("Repo: %s", config.repo_url or "(not configured)")

    try:
        await server.serve()
    finally:
        await bot_manager.stop_bot()


def cli() -> None:
    """CLI entry point for the bot."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    cli()
