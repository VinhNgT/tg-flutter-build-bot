"""Chat ID whitelist filter for Telegram bot."""

from __future__ import annotations

from telegram import Update
from telegram.ext import filters

from ..store import Store


class ChatWhitelistFilter(filters.MessageFilter):
    """Filter that only allows messages from whitelisted chat IDs.

    Reads the allowed list from the store on each check so that
    Web UI changes take effect immediately.
    """

    def __init__(self, store: Store) -> None:
        super().__init__()
        self._store = store

    def filter(self, message) -> bool:
        config = self._store.get_effective_config()
        if not config.allowed_chat_ids:
            # If no whitelist configured, allow all (for initial setup)
            return True
        return message.chat_id in config.allowed_chat_ids
