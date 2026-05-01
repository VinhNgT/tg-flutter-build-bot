"""Telegram command handlers for the build bot."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from ..builder.service import BuilderError, BuilderService
from ..config import extract_project_name, get_effective_drive_folder_name
from ..drive.uploader import DriveUploader
from ..store import Store

logger = logging.getLogger(__name__)

# Global build state
_build_lock = asyncio.Lock()
_last_build_time: float = 0.0


async def start_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /start command — welcome message."""
    await update.message.reply_text(
        "🤖 *Flutter Build Bot*\n\n"
        "Available commands:\n"
        "▸ `/build` — Build latest commit on main\n"
        "▸ `/build <branch>` — Build latest commit on a branch\n"
        "▸ `/build <hash>` — Build a specific commit\n"
        "▸ `/status` — Current build status\n"
        "▸ `/recent` — Recent build history",
        parse_mode="Markdown",
    )


async def build_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
    build_service: BuilderService,
    drive_uploader: DriveUploader,
) -> None:
    """Handle /build command — trigger a Flutter build.

    Flow:
    1. Check build lock → reject if busy
    2. Check cooldown → reply with remaining time
    3. Resolve commit → check cache → return existing link
    4. Clone → build → upload → reply with link
    """
    global _last_build_time

    config = store.get_effective_config()

    if not config.repo_url:
        await update.message.reply_text(
            "❌ No repository URL configured. "
            "Set it via the Web UI or REPO_URL env var."
        )
        return

    # Parse the optional ref argument
    ref = "main"
    if context.args:
        ref = context.args[0]

    # 1. Check build lock
    if _build_lock.locked():
        current = build_service.current_build
        short = current[:7] if current else "unknown"
        await update.message.reply_text(
            f"🚧 A build is already in progress (commit `{short}`). "
            f"Please wait.",
            parse_mode="Markdown",
        )
        return

    # 2. Check cooldown
    elapsed = time.time() - _last_build_time
    remaining = config.cooldown_seconds - elapsed
    if remaining > 0 and _last_build_time > 0:
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        await update.message.reply_text(
            f"⏳ Cooldown active. Next build available in "
            f"{mins}m {secs}s."
        )
        return

    # 3. Resolve the target commit
    await update.message.reply_text(
        f"🔍 Resolving `{ref}`...", parse_mode="Markdown"
    )

    try:
        commit_hash = await build_service.resolve_remote_commit(
            config.repo_url, ref
        )
    except BuilderError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    # 4. Check build cache
    existing = store.find_build_by_commit(commit_hash)
    if existing and existing.status == "success":
        # Verify Drive link is still valid
        oauth = store.get_oauth_config()
        if existing.drive_file_id and oauth.refresh_token:
            still_exists = await drive_uploader.check_file_exists(
                existing.drive_file_id, oauth
            )
            if still_exists:
                short = existing.short_hash
                await update.message.reply_text(
                    f"✅ This commit was already built!\n\n"
                    f"📦 `{existing.filename}`\n"
                    f"🔗 [Download APK]({existing.drive_link})",
                    parse_mode="Markdown",
                )
                return
            else:
                # Drive file deleted — try re-upload from local copy
                local_path = store.get_local_artifact_path(existing.filename)
                if local_path:
                    await update.message.reply_text(
                        "♻️ Drive file was deleted. Re-uploading from local copy..."
                    )
                    try:
                        folder_name = get_effective_drive_folder_name(
                            config.drive_folder_name, config.repo_url
                        )
                        folder_id = await drive_uploader.ensure_folder(
                            oauth, folder_name
                        )
                        file_id, link = await drive_uploader.upload_file(
                            str(local_path),
                            existing.filename,
                            oauth,
                            folder_id,
                        )
                        await store.update_build(
                            commit_hash,
                            drive_file_id=file_id,
                            drive_link=link,
                        )
                        await update.message.reply_text(
                            f"✅ Re-uploaded successfully!\n\n"
                            f"📦 `{existing.filename}`\n"
                            f"🔗 [Download APK]({link})",
                            parse_mode="Markdown",
                        )
                        return
                    except Exception as e:
                        logger.warning("Re-upload failed: %s", e)
                        # Fall through to rebuild

        elif existing.drive_link:
            # No OAuth to verify, just return the link
            await update.message.reply_text(
                f"✅ This commit was already built!\n\n"
                f"📦 `{existing.filename}`\n"
                f"🔗 [Download APK]({existing.drive_link})",
                parse_mode="Markdown",
            )
            return

    if existing and existing.status == "building":
        await update.message.reply_text(
            "🔨 This commit is currently being built..."
        )
        return

    # 5. Acquire lock and build
    async with _build_lock:
        build_service._current_build = commit_hash
        repo_path: str | None = None

        try:
            project_name = extract_project_name(config.repo_url)
            short_hash = commit_hash[:7]

            # Create initial build record
            from ..config import BuildRecord
            from datetime import datetime, timezone

            filename = build_service.generate_artifact_name(
                project_name, commit_hash
            )
            record = BuildRecord(
                commit_hash=commit_hash,
                short_hash=short_hash,
                filename=filename,
                timestamp=datetime.now(timezone.utc).isoformat(),
                status="building",
            )
            await store.add_build(record)

            # Clone
            await update.message.reply_text(
                f"📥 Cloning repository (ref: `{ref}`)...",
                parse_mode="Markdown",
            )
            repo_path, resolved_hash = await build_service.clone_repo(
                config.repo_url, ref
            )

            # Update commit hash if it was resolved from a branch
            if resolved_hash != commit_hash:
                # Remove the placeholder record and re-check cache
                await store.delete_build(commit_hash)
                commit_hash = resolved_hash
                short_hash = commit_hash[:7]

                # Check if this resolved hash already exists
                existing = store.find_build_by_commit(commit_hash)
                if existing and existing.status == "success":
                    build_service.cleanup(repo_path)
                    await update.message.reply_text(
                        f"✅ Latest commit already built!\n\n"
                        f"📦 `{existing.filename}`\n"
                        f"🔗 [Download APK]({existing.drive_link})",
                        parse_mode="Markdown",
                    )
                    return

                filename = build_service.generate_artifact_name(
                    project_name, commit_hash
                )
                record = BuildRecord(
                    commit_hash=commit_hash,
                    short_hash=short_hash,
                    filename=filename,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    status="building",
                )
                await store.add_build(record)

            build_service._current_build = commit_hash

            # Build
            await update.message.reply_text(
                f"🔨 Building `{short_hash}`...\n"
                f"Command: `{config.build_command}`",
                parse_mode="Markdown",
            )
            await build_service.run_build(repo_path, config.build_command)

            # Locate artifact
            artifact_path = build_service.get_artifact_path(
                repo_path, config.build_output_path
            )

            # Copy to local builds directory
            store.copy_artifact_to_builds(artifact_path, filename)

            # Upload to Drive
            oauth = store.get_oauth_config()
            drive_link = ""
            drive_file_id = ""

            if oauth.refresh_token:
                await update.message.reply_text("☁️ Uploading to Google Drive...")
                try:
                    folder_name = get_effective_drive_folder_name(
                        config.drive_folder_name, config.repo_url
                    )
                    folder_id = await drive_uploader.ensure_folder(
                        oauth, folder_name
                    )
                    drive_file_id, drive_link = await drive_uploader.upload_file(
                        artifact_path, filename, oauth, folder_id
                    )
                except Exception as e:
                    logger.error("Drive upload failed: %s", e)
                    await update.message.reply_text(
                        f"⚠️ Drive upload failed: {e}\n"
                        f"APK is saved locally."
                    )
            else:
                await update.message.reply_text(
                    "⚠️ Google Drive not connected. "
                    "APK saved locally only. Connect via Web UI."
                )

            # Update build record
            await store.update_build(
                commit_hash,
                status="success",
                drive_file_id=drive_file_id,
                drive_link=drive_link,
            )

            # Prune old builds
            async def _drive_delete(fid: str) -> None:
                await drive_uploader.delete_file(fid, oauth)

            await store.prune_builds(
                config.max_builds,
                drive_delete_fn=_drive_delete if oauth.refresh_token else None,
            )

            # Success reply
            if drive_link:
                await update.message.reply_text(
                    f"✅ Build successful!\n\n"
                    f"📦 `{filename}`\n"
                    f"🔗 [Download APK]({drive_link})",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"✅ Build successful!\n\n"
                    f"📦 `{filename}`\n"
                    f"(Saved locally — connect Google Drive via Web UI to get download links)",
                    parse_mode="Markdown",
                )

        except BuilderError as e:
            await store.update_build(commit_hash, status="failed")
            await update.message.reply_text(f"❌ Build failed:\n```\n{e}\n```", parse_mode="Markdown")

        except Exception as e:
            logger.exception("Unexpected error during build")
            await store.update_build(commit_hash, status="failed")
            await update.message.reply_text(
                f"❌ Unexpected error: {e}"
            )

        finally:
            # Cleanup
            if repo_path:
                build_service.cleanup(repo_path)
            build_service._current_build = None
            _last_build_time = time.time()


async def status_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
    build_service: BuilderService,
) -> None:
    """Handle /status command — show current build status."""
    global _last_build_time
    config = store.get_effective_config()

    lines = ["📊 *Bot Status*\n"]

    # Build status
    if build_service.is_building:
        current = build_service.current_build
        short = current[:7] if current else "unknown"
        lines.append(f"🔨 Currently building: `{short}`")
    else:
        lines.append("✅ Idle — ready for builds")

    # Cooldown
    if _last_build_time > 0:
        elapsed = time.time() - _last_build_time
        remaining = config.cooldown_seconds - elapsed
        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            lines.append(f"⏳ Cooldown: {mins}m {secs}s remaining")
        else:
            lines.append("✅ No cooldown — ready")
    else:
        lines.append("✅ No cooldown — ready")

    # Config summary
    lines.append("\n📋 *Config*")
    lines.append(f"▸ Repo: `{config.repo_url or 'Not set'}`")
    lines.append(f"▸ Build cmd: `{config.build_command}`")
    lines.append(f"▸ Cooldown: {config.cooldown_seconds}s")
    lines.append(f"▸ Max builds: {config.max_builds}")

    # OAuth status
    oauth = store.get_oauth_config()
    if oauth.refresh_token:
        lines.append("▸ Drive: ✅ Connected")
    else:
        lines.append("▸ Drive: ❌ Not connected")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


async def recent_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    store: Store,
) -> None:
    """Handle /recent command — list recent builds."""
    builds = store.get_builds()

    if not builds:
        await update.message.reply_text("📭 No builds yet.")
        return

    lines = ["📦 *Recent Builds*\n"]
    for b in builds[:5]:
        status_icon = {
            "success": "✅",
            "failed": "❌",
            "building": "🔨",
        }.get(b.status, "❓")

        line = f"{status_icon} `{b.short_hash}` — {b.timestamp[:16]}"
        if b.drive_link:
            line += f" — [Download]({b.drive_link})"
        lines.append(line)

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )
