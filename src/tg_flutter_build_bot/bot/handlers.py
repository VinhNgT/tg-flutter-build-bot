"""Telegram command handlers — thin trigger layer for Jenkins builds."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from .context import BotContext

logger = logging.getLogger(__name__)

# ConversationHandler states for /connect_drive
AWAITING_CODE = 0


def _get_ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    """Retrieve the shared BotContext from bot_data."""
    return context.bot_data["bot_context"]


# ------------------------------------------------------------------
# /start
# ------------------------------------------------------------------


async def start_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /start command — welcome message."""
    assert update.message
    await update.message.reply_text(
        "🤖 *Flutter Build Bot*\n\n"
        "Available commands:\n"
        "▸ `/build` — Build latest commit on main\n"
        "▸ `/build <branch>` — Build latest on a branch\n"
        "▸ `/build <hash>` — Build a specific commit\n"
        "▸ `/status` — Current build status\n"
        "▸ `/recent` — Recent build history\n"
        "▸ `/connect_drive` — Connect Google Drive",
        parse_mode="Markdown",
    )


# ------------------------------------------------------------------
# /build
# ------------------------------------------------------------------


async def build_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Trigger a Jenkins build and track for notification."""
    assert update.message
    assert update.effective_chat
    ctx = _get_ctx(context)
    config = ctx.config
    chat_id = update.effective_chat.id

    # Check allowed chats
    if chat_id not in config.allowed_chat_ids:
        await update.message.reply_text("❌ Unauthorized.")
        return

    ref = context.args[0] if context.args else "main"

    # Check Drive connection
    if not ctx.drive.is_connected():
        await update.message.reply_text(
            "❌ Google Drive is not connected.\n"
            "Use /connect\\_drive to set up uploads first."
        )
        return

    # Trigger Jenkins build
    queue_id = await ctx.jenkins.trigger_build(
        branch=ref,
        callback_url=config.bot_callback_url,
    )

    if queue_id is None:
        await update.message.reply_text(
            "❌ Failed to trigger Jenkins build.\n"
            "Check bot logs for details."
        )
        return

    ctx.add_pending(queue_id, chat_id, ref)

    await update.message.reply_text(
        f"🚀 Build triggered for `{ref}`\n"
        f"⏳ You'll be notified when it completes.",
        parse_mode="Markdown",
    )


# ------------------------------------------------------------------
# /status
# ------------------------------------------------------------------


async def status_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Query Jenkins for current build status."""
    assert update.message
    assert update.effective_chat
    ctx = _get_ctx(context)
    chat_id = update.effective_chat.id

    if chat_id not in ctx.config.allowed_chat_ids:
        await update.message.reply_text("❌ Unauthorized.")
        return

    lines = ["📊 *Bot Status*\n"]

    # Drive connection
    if ctx.drive.is_connected():
        lines.append("▸ Drive: ✅ Connected")
    else:
        lines.append("▸ Drive: ❌ Not connected")

    # Pending builds
    pending_count = len(ctx._pending)
    if pending_count > 0:
        lines.append(f"▸ Pending builds: {pending_count}")
    else:
        lines.append("▸ Pending builds: None")

    # Jenkins connection check — try to get recent builds
    try:
        builds = await ctx.jenkins.get_recent_builds(count=1)
        if builds:
            last = builds[0]
            result = last.get("result") or "IN PROGRESS"
            lines.append("▸ Jenkins: ✅ Connected")
            lines.append(f"▸ Last build: #{last['number']} — {result}")
        else:
            lines.append("▸ Jenkins: ✅ Connected (no builds yet)")
    except Exception:
        lines.append("▸ Jenkins: ❌ Unreachable")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


# ------------------------------------------------------------------
# /recent
# ------------------------------------------------------------------


async def recent_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Query Jenkins for recent build history."""
    assert update.message
    assert update.effective_chat
    ctx = _get_ctx(context)
    chat_id = update.effective_chat.id

    if chat_id not in ctx.config.allowed_chat_ids:
        await update.message.reply_text("❌ Unauthorized.")
        return

    try:
        builds = await ctx.jenkins.get_recent_builds(count=5)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to query Jenkins: {e}")
        return

    if not builds:
        await update.message.reply_text("📭 No builds yet.")
        return

    lines = ["📦 *Recent Builds*\n"]
    for b in builds:
        number = b.get("number", "?")
        result = b.get("result") or "IN PROGRESS"
        ts = b.get("timestamp", 0)

        icon = {
            "SUCCESS": "✅",
            "FAILURE": "❌",
            "ABORTED": "⏹️",
            "IN PROGRESS": "🔨",
        }.get(result, "❓")

        # Convert Jenkins timestamp (ms) to readable date
        if ts:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        else:
            date_str = "unknown"

        lines.append(f"{icon} #{number} — {result} — {date_str}")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


# ------------------------------------------------------------------
# /connect_drive (ConversationHandler)
# ------------------------------------------------------------------


async def connect_drive_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the Google Drive OAuth flow."""
    assert update.message
    assert update.effective_chat
    ctx = _get_ctx(context)
    chat_id = update.effective_chat.id

    if chat_id not in ctx.config.allowed_chat_ids:
        await update.message.reply_text("❌ Unauthorized.")
        return ConversationHandler.END

    if ctx.drive.is_connected():
        await update.message.reply_text(
            "✅ Google Drive is already connected.\n"
            "Send /connect\\_drive again and paste a new code to "
            "re-authorize."
        )

    auth_url = ctx.drive.get_auth_url()

    await update.message.reply_text(
        "🔗 *Authorize Google Drive access:*\n\n"
        f"[Click here to authorize]({auth_url})\n\n"
        "After authorizing, your browser will show "
        '"can\'t reach this page".\n'
        "Copy the `code=` value from the URL bar and send it here.\n\n"
        "_Send /cancel to abort._",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    return AWAITING_CODE


async def connect_drive_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Receive the OAuth code and exchange it for tokens."""
    assert update.message
    assert update.message.text
    ctx = _get_ctx(context)
    code = update.message.text.strip()

    try:
        ctx.drive.exchange_code(code)
        await update.message.reply_text(
            "✅ Google Drive connected successfully!\n"
            "You can now use /build to trigger builds."
        )
    except Exception as e:
        logger.exception("OAuth code exchange failed")
        await update.message.reply_text(
            f"❌ Failed to connect: {e}\n"
            f"Try /connect\\_drive again."
        )

    return ConversationHandler.END


async def connect_drive_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Cancel the OAuth flow."""
    assert update.message
    await update.message.reply_text("❌ OAuth flow cancelled.")
    return ConversationHandler.END
