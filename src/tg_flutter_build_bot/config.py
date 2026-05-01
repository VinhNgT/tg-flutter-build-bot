"""Configuration models and environment resolution logic."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class BotConfig(BaseModel):
    """Main bot configuration — all fields have sensible defaults."""

    telegram_token: str = ""
    repo_url: str = ""
    build_command: str = "flutter build apk --release"
    build_output_path: str = "build/app/outputs/flutter-apk/app-release.apk"
    allowed_chat_ids: list[int] = Field(default_factory=list)
    cooldown_seconds: int = 300
    max_builds: int = 3
    drive_folder_name: str = ""  # If empty, defaults to "{projectName}-tg-flutter-build-bot"
    web_port: int = 8080
    gitlab_pat: str = ""  # Optional GitLab Personal Access Token for private repos


class OAuthConfig(BaseModel):
    """Google OAuth2 tokens for Drive access."""

    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    access_token: str = ""


class BuildRecord(BaseModel):
    """A single build history entry."""

    commit_hash: str  # Full 40-char SHA (used for deduplication)
    short_hash: str  # First 7 chars (used in filename/display only)
    filename: str  # e.g. "tendoo-mall-20260501-1130-abc1234.apk"
    timestamp: str  # ISO format
    drive_file_id: str = ""
    drive_link: str = ""
    status: str = "building"  # "building", "success", "failed"


# ---------------------------------------------------------------------------
# Env var mapping
# ---------------------------------------------------------------------------

ENV_MAP: dict[str, str] = {
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "repo_url": "REPO_URL",
    "build_command": "BUILD_COMMAND",
    "build_output_path": "BUILD_OUTPUT_PATH",
    "allowed_chat_ids": "ALLOWED_CHAT_IDS",  # comma-separated
    "cooldown_seconds": "COOLDOWN_SECONDS",
    "max_builds": "MAX_BUILDS",
    "drive_folder_name": "DRIVE_FOLDER_NAME",
    "web_port": "WEB_PORT",
    "gitlab_pat": "GITLAB_PAT",
}

OAUTH_ENV_MAP: dict[str, str] = {
    "client_id": "GOOGLE_CLIENT_ID",
    "client_secret": "GOOGLE_CLIENT_SECRET",
}

# Fields whose values must never be sent to the front-end HTML.
SECRET_FIELDS: set[str] = {"telegram_token", "gitlab_pat"}
OAUTH_SECRET_FIELDS: set[str] = {"client_secret"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_project_name(repo_url: str) -> str:
    """Extract project name from git URL.

    - Takes the last path segment
    - Strips `.git` suffix
    - Replaces underscores with hyphens
    - Falls back to 'app' if URL is empty or unparseable.

    Examples:
        'http://52.74.56.51/tendoo/frontend/mobile/flutter/tendoo_mall.git'
            -> 'tendoo-mall'
        'git@github.com:user/my_app.git'
            -> 'my-app'
    """
    if not repo_url:
        return "app"
    try:
        # Handle both HTTP and SSH URLs
        if "://" in repo_url:
            parsed = urlparse(repo_url)
            path = parsed.path
        else:
            # SSH style: git@host:user/repo.git
            path = repo_url.split(":", 1)[-1] if ":" in repo_url else repo_url

        # Get last segment, strip .git
        name = path.rstrip("/").rsplit("/", 1)[-1]
        name = re.sub(r"\.git$", "", name)
        name = name.replace("_", "-")
        return name or "app"
    except Exception:
        return "app"


def get_effective_drive_folder_name(
    drive_folder_name: str, repo_url: str
) -> str:
    """Resolve the Drive folder name, using project name as fallback."""
    if drive_folder_name:
        return drive_folder_name
    project_name = extract_project_name(repo_url)
    return f"{project_name}-tg-flutter-build-bot"


def inject_pat_into_url(repo_url: str, pat: str) -> str:
    """Inject a GitLab PAT into an HTTP(S) repo URL for authenticated cloning.

    Transforms:
        https://gitlab.com/user/repo.git
        → https://oauth2:<pat>@gitlab.com/user/repo.git

    Returns the URL unchanged if it's not HTTP(S) or if PAT is empty.
    """
    if not pat or not repo_url:
        return repo_url

    parsed = urlparse(repo_url)
    if parsed.scheme not in ("http", "https"):
        return repo_url

    # Replace or set the userinfo
    netloc = f"oauth2:{pat}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    return parsed._replace(netloc=netloc).geturl()


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

_DEFAULTS = BotConfig()


def _is_default(field_name: str, value: object) -> bool:
    """Check if a value is the default for a given field."""
    return value == getattr(_DEFAULTS, field_name)


def _parse_env_value(field_name: str, raw: str) -> object:
    """Convert a raw env string to the correct type for the field."""
    if field_name == "allowed_chat_ids":
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    if field_name in ("cooldown_seconds", "max_builds", "web_port"):
        return int(raw)
    return raw


def resolve_field(
    field_name: str,
    saved_value: object,
    env_map: dict[str, str] | None = None,
) -> tuple[object, str]:
    """Resolve a single config field through the precedence chain.

    Returns (resolved_value, source) where source is one of:
    'saved', 'env', 'dotenv', 'default'.

    Note: python-dotenv loads .env values into os.environ but does NOT
    override existing env vars. We distinguish env vs dotenv by checking
    if the var was set before dotenv loaded — but since dotenv merges into
    os.environ, we treat them uniformly as 'env' here. The store layer
    tracks the distinction via _dotenv_keys.
    """
    if env_map is None:
        env_map = ENV_MAP

    # 1. Saved value (from Web UI / config.json)
    if not _is_default(field_name, saved_value):
        return saved_value, "saved"

    # 2. Env var (includes .env loaded by python-dotenv)
    env_var = env_map.get(field_name)
    if env_var:
        raw = os.environ.get(env_var)
        if raw is not None and raw != "":
            return _parse_env_value(field_name, raw), "env"

    # 3. Hardcoded default
    return getattr(_DEFAULTS, field_name), "default"


def resolve_config(saved: BotConfig) -> tuple[BotConfig, dict[str, str]]:
    """Resolve all config fields through the precedence chain.

    Returns (effective_config, sources_dict) where sources_dict maps
    field names to their source ('saved', 'env', 'dotenv', 'default').
    """
    resolved: dict[str, object] = {}
    sources: dict[str, str] = {}

    for field_name in ENV_MAP:
        saved_value = getattr(saved, field_name)
        value, source = resolve_field(field_name, saved_value)
        resolved[field_name] = value
        sources[field_name] = source

    return BotConfig(**resolved), sources


def resolve_oauth(saved: OAuthConfig) -> OAuthConfig:
    """Resolve OAuth config from saved values and env vars."""
    data: dict[str, str] = {}
    for field_name, env_var in OAUTH_ENV_MAP.items():
        saved_value = getattr(saved, field_name)
        if saved_value:
            data[field_name] = saved_value
        else:
            data[field_name] = os.environ.get(env_var, "")

    # refresh_token and access_token only come from saved config
    data["refresh_token"] = saved.refresh_token
    data["access_token"] = saved.access_token

    return OAuthConfig(**data)


def resolve_oauth_sources(saved: OAuthConfig) -> dict[str, str]:
    """Return a dict mapping OAuth field names to their source.

    Sources: 'saved', 'env', 'default'.
    """
    sources: dict[str, str] = {}
    for field_name, env_var in OAUTH_ENV_MAP.items():
        saved_value = getattr(saved, field_name)
        if saved_value:
            sources[field_name] = "saved"
        elif os.environ.get(env_var, ""):
            sources[field_name] = "env"
        else:
            sources[field_name] = "default"
    return sources
