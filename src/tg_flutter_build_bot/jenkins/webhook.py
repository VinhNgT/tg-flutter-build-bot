"""Webhook server — receives build results from Jenkins."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from aiohttp.multipart import BodyPartReader

if TYPE_CHECKING:
    from ..bot.context import BotContext

logger = logging.getLogger(__name__)

WORK_DIR = Path("data/work")


def create_webhook_app(bot_context: BotContext) -> web.Application:
    """Create the aiohttp web app for Jenkins callbacks."""
    app = web.Application()
    app["bot_ctx"] = bot_context
    app.router.add_post("/webhook/build-complete", handle_build_complete)
    app.router.add_get("/health", handle_health)
    return app


async def handle_health(request: web.Request) -> web.Response:
    """Simple health check endpoint."""
    return web.Response(text="OK")


async def handle_build_complete(request: web.Request) -> web.Response:
    """Handle Jenkins build completion callback.

    Expected multipart POST:
      - Field 'metadata': JSON with queue_id, status, commit_hash, logs
      - Field 'artifact': The built APK file (only on success)
    """
    ctx: BotContext = request.app["bot_ctx"]

    reader = await request.multipart()
    metadata = None
    artifact_path = None

    async for part in reader:
        if not isinstance(part, BodyPartReader):
            continue

        if part.name == "metadata":
            raw = await part.read(decode=True)
            metadata = json.loads(raw)

        elif part.name == "artifact":
            WORK_DIR.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=".apk", dir=str(WORK_DIR)
            )
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                tmp.write(chunk)
            tmp.close()
            artifact_path = tmp.name

    if not metadata:
        return web.Response(status=400, text="Missing metadata")

    queue_id = metadata.get("queue_id")
    status = metadata.get("status", "unknown")
    commit_hash = metadata.get("commit_hash", "unknown")

    logger.info(
        "Build callback: queue_id=%s, status=%s, commit=%s",
        queue_id,
        status,
        commit_hash,
    )

    # Look up pending build (returns None if not triggered via Telegram)
    pending = ctx.consume_pending(queue_id)

    if pending is None:
        logger.info(
            "No pending Telegram request for queue_id=%s — ignoring.",
            queue_id,
        )
        if artifact_path:
            Path(artifact_path).unlink(missing_ok=True)
        return web.Response(
            text="OK (ignored — not triggered via Telegram)"
        )

    # Telegram-triggered build — process the result
    if status == "success" and artifact_path:
        await ctx.on_build_success(pending, metadata, artifact_path)
    else:
        await ctx.on_build_failure(pending, metadata)
        if artifact_path:
            Path(artifact_path).unlink(missing_ok=True)

    return web.Response(text="OK")
