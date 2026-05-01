"""Persistent JSON store for configuration and build history."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import (
    BotConfig,
    BuildRecord,
    OAuthConfig,
    resolve_config,
    resolve_oauth,
)

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
CONFIG_FILE = DATA_DIR / "config.json"
BUILDS_FILE = DATA_DIR / "builds.json"
BUILDS_DIR = DATA_DIR / "builds"


class Store:
    """Thread-safe JSON-based persistent store.

    Manages two JSON files:
    - data/config.json: Bot configuration + OAuth tokens
    - data/builds.json: Build history records

    And a directory:
    - data/builds/: Local APK copies for re-upload recovery
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ensure_dirs()
        self._saved_config = self._load_config()
        self._oauth_config = self._load_oauth()
        self._builds = self._load_builds()

    # ------------------------------------------------------------------
    # Filesystem setup
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        """Create data directories if they don't exist."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BUILDS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def _load_config(self) -> BotConfig:
        """Load saved config from disk, or return defaults."""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                bot_data = data.get("bot", {})
                return BotConfig(**bot_data)
            except Exception:
                logger.warning("Failed to load config.json, using defaults")
        return BotConfig()

    def _load_oauth(self) -> OAuthConfig:
        """Load OAuth config from disk."""
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                oauth_data = data.get("oauth", {})
                return OAuthConfig(**oauth_data)
            except Exception:
                logger.warning("Failed to load OAuth config, using defaults")
        return OAuthConfig()

    def _save_config_to_disk(self) -> None:
        """Write current config + OAuth to disk."""
        data = {
            "bot": self._saved_config.model_dump(),
            "oauth": self._oauth_config.model_dump(),
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2))

    def get_saved_config(self) -> BotConfig:
        """Return the raw saved config (before env resolution)."""
        return self._saved_config.model_copy()

    def get_effective_config(self) -> BotConfig:
        """Return the fully resolved config (saved → env → .env → default)."""
        config, _ = resolve_config(self._saved_config)
        return config

    def get_config_sources(self) -> dict[str, str]:
        """Return a dict mapping field names to their source.

        Sources: 'saved', 'env', 'dotenv', 'default'.
        Used by the Web UI to display source badges.
        """
        _, sources = resolve_config(self._saved_config)
        return sources

    async def save_config(self, updates: dict[str, object]) -> None:
        """Save Web UI overrides to config.json.

        Only non-empty values are saved. Empty/None values are removed
        from the saved config (falling back to env → .env → default).
        """
        async with self._lock:
            current = self._saved_config.model_dump()

            for key, value in updates.items():
                if key not in current:
                    continue
                # Empty string or None means "clear override"
                if value is None or value == "" or value == []:
                    current[key] = getattr(BotConfig(), key)  # Reset to default
                else:
                    current[key] = value

            self._saved_config = BotConfig(**current)
            self._save_config_to_disk()

    # ------------------------------------------------------------------
    # OAuth persistence
    # ------------------------------------------------------------------

    def get_oauth_config(self) -> OAuthConfig:
        """Return the resolved OAuth config."""
        return resolve_oauth(self._oauth_config)

    def get_saved_oauth(self) -> OAuthConfig:
        """Return the raw saved OAuth config."""
        return self._oauth_config.model_copy()

    async def save_oauth(self, oauth: OAuthConfig) -> None:
        """Save OAuth tokens to config.json."""
        async with self._lock:
            self._oauth_config = oauth
            self._save_config_to_disk()

    async def clear_oauth(self) -> None:
        """Clear all OAuth tokens."""
        async with self._lock:
            self._oauth_config = OAuthConfig(
                client_id=self._oauth_config.client_id,
                client_secret=self._oauth_config.client_secret,
            )
            self._save_config_to_disk()

    # ------------------------------------------------------------------
    # Build history persistence
    # ------------------------------------------------------------------

    def _load_builds(self) -> list[BuildRecord]:
        """Load build history from disk."""
        if BUILDS_FILE.exists():
            try:
                data = json.loads(BUILDS_FILE.read_text())
                return [BuildRecord(**b) for b in data]
            except Exception:
                logger.warning("Failed to load builds.json, starting fresh")
        return []

    def _save_builds_to_disk(self) -> None:
        """Write build history to disk."""
        data = [b.model_dump() for b in self._builds]
        BUILDS_FILE.write_text(json.dumps(data, indent=2))

    def get_builds(self) -> list[BuildRecord]:
        """Return all build records, newest first."""
        return list(reversed(self._builds))

    def find_build_by_commit(self, commit_hash: str) -> BuildRecord | None:
        """Find a build by its full commit hash."""
        for build in self._builds:
            if build.commit_hash == commit_hash:
                return build
        return None

    async def add_build(self, record: BuildRecord) -> None:
        """Add a build record and persist."""
        async with self._lock:
            self._builds.append(record)
            self._save_builds_to_disk()

    async def update_build(
        self, commit_hash: str, **updates: object
    ) -> BuildRecord | None:
        """Update fields on an existing build record."""
        async with self._lock:
            for build in self._builds:
                if build.commit_hash == commit_hash:
                    for key, value in updates.items():
                        if hasattr(build, key):
                            setattr(build, key, value)
                    self._save_builds_to_disk()
                    return build
            return None

    async def delete_build(self, commit_hash: str) -> BuildRecord | None:
        """Delete a build record and its local APK file."""
        async with self._lock:
            for i, build in enumerate(self._builds):
                if build.commit_hash == commit_hash:
                    removed = self._builds.pop(i)
                    self._save_builds_to_disk()

                    # Delete local APK
                    local_path = BUILDS_DIR / removed.filename
                    if local_path.exists():
                        local_path.unlink()
                        logger.info("Deleted local APK: %s", local_path)

                    return removed
            return None

    async def prune_builds(
        self,
        max_builds: int,
        drive_delete_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> list[BuildRecord]:
        """Enforce the max build limit.

        Deletes the oldest build records, their local APK files,
        and optionally their Google Drive files.

        Returns the list of pruned records.
        """
        pruned: list[BuildRecord] = []

        async with self._lock:
            while len(self._builds) > max_builds:
                oldest = self._builds.pop(0)
                pruned.append(oldest)

                # Delete local APK
                local_path = BUILDS_DIR / oldest.filename
                if local_path.exists():
                    local_path.unlink()
                    logger.info("Pruned local APK: %s", local_path)

            if pruned:
                self._save_builds_to_disk()

        # Delete from Drive outside the lock (network I/O)
        if drive_delete_fn and pruned:
            for record in pruned:
                if record.drive_file_id:
                    try:
                        await drive_delete_fn(record.drive_file_id)
                        logger.info("Pruned Drive file: %s", record.drive_file_id)
                    except Exception:
                        logger.warning(
                            "Failed to delete Drive file: %s",
                            record.drive_file_id,
                        )

        return pruned

    def copy_artifact_to_builds(self, source_path: str, filename: str) -> Path:
        """Copy a built APK to the local builds directory.

        Returns the destination path.
        """
        dest = BUILDS_DIR / filename
        shutil.copy2(source_path, dest)
        logger.info("Copied artifact to %s", dest)
        return dest

    def get_local_artifact_path(self, filename: str) -> Path | None:
        """Get the local path of a build artifact, if it exists."""
        path = BUILDS_DIR / filename
        return path if path.exists() else None
