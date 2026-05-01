"""Web UI routes for the admin dashboard."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import get_effective_drive_folder_name
from ..drive.uploader import DriveUploader
from ..store import Store

logger = logging.getLogger(__name__)


def create_routes(
    store: Store,
    drive_uploader: DriveUploader,
    templates: Jinja2Templates,
) -> APIRouter:
    """Create all web routes bound to the given store and uploader."""
    router = APIRouter()

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        config = store.get_effective_config()
        builds = store.get_builds()
        oauth = store.get_oauth_config()

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "config": config,
                "builds": builds[:5],
                "oauth_connected": bool(oauth.refresh_token),
                "drive_folder": get_effective_drive_folder_name(
                    config.drive_folder_name, config.repo_url
                ),
            },
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @router.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        config = store.get_effective_config()
        sources = store.get_config_sources()
        saved = store.get_saved_config()
        oauth = store.get_saved_oauth()

        return templates.TemplateResponse(
            request,
            "config.html",
            {
                "config": config,
                "sources": sources,
                "saved": saved,
                "oauth": oauth,
            },
        )

    @router.post("/config")
    async def save_config(request: Request):
        form = await request.form()

        updates = {}
        for key in (
            "telegram_token",
            "repo_url",
            "build_command",
            "build_output_path",
            "drive_folder_name",
        ):
            value = form.get(key, "")
            updates[key] = value if value else ""

        # Integer fields
        for key in ("cooldown_seconds", "max_builds", "web_port"):
            value = form.get(key, "")
            if value:
                try:
                    updates[key] = int(value)
                except ValueError:
                    pass
            else:
                updates[key] = None  # Clear override

        # Chat IDs (comma-separated)
        chat_ids_raw = form.get("allowed_chat_ids", "")
        if chat_ids_raw:
            try:
                updates["allowed_chat_ids"] = [
                    int(x.strip())
                    for x in chat_ids_raw.split(",")
                    if x.strip()
                ]
            except ValueError:
                pass
        else:
            updates["allowed_chat_ids"] = []

        await store.save_config(updates)

        # Save OAuth client_id/secret separately
        oauth = store.get_saved_oauth()
        client_id = form.get("client_id", "") or oauth.client_id
        client_secret = form.get("client_secret", "") or oauth.client_secret

        from ..config import OAuthConfig

        await store.save_oauth(
            OAuthConfig(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=oauth.refresh_token,
                access_token=oauth.access_token,
            )
        )

        return RedirectResponse(url="/config?saved=1", status_code=303)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    @router.get("/oauth", response_class=HTMLResponse)
    async def oauth_page(request: Request):
        oauth = store.get_oauth_config()
        config = store.get_effective_config()

        return templates.TemplateResponse(
            request,
            "oauth.html",
            {
                "oauth": oauth,
                "connected": bool(oauth.refresh_token),
                "has_credentials": bool(
                    oauth.client_id and oauth.client_secret
                ),
                "drive_folder": get_effective_drive_folder_name(
                    config.drive_folder_name, config.repo_url
                ),
            },
        )

    @router.get("/oauth/login")
    async def oauth_login(request: Request):
        oauth = store.get_oauth_config()
        if not oauth.client_id or not oauth.client_secret:
            return RedirectResponse(url="/oauth?error=no_credentials")

        redirect_uri = str(request.url_for("oauth_callback"))
        auth_url = drive_uploader.get_auth_url(
            oauth.client_id, oauth.client_secret, redirect_uri
        )
        return RedirectResponse(url=auth_url)

    @router.get("/oauth/callback")
    async def oauth_callback(request: Request):
        code = request.query_params.get("code")
        error = request.query_params.get("error")

        if error:
            return RedirectResponse(url=f"/oauth?error={error}")

        if not code:
            return RedirectResponse(url="/oauth?error=no_code")

        oauth = store.get_oauth_config()
        redirect_uri = str(request.url_for("oauth_callback"))

        try:
            new_oauth = drive_uploader.exchange_code(
                code, oauth.client_id, oauth.client_secret, redirect_uri
            )
            await store.save_oauth(new_oauth)
            return RedirectResponse(url="/oauth?success=1")
        except Exception as e:
            logger.error("OAuth exchange failed: %s", e)
            return RedirectResponse(
                url="/oauth?error=exchange_failed"
            )

    @router.post("/oauth/disconnect")
    async def oauth_disconnect(request: Request):
        await store.clear_oauth()
        return RedirectResponse(url="/oauth?disconnected=1", status_code=303)

    # ------------------------------------------------------------------
    # Builds
    # ------------------------------------------------------------------

    @router.get("/builds", response_class=HTMLResponse)
    async def builds_page(request: Request):
        builds = store.get_builds()
        return templates.TemplateResponse(
            request,
            "builds.html",
            {"builds": builds},
        )

    @router.post("/builds/{commit_hash}/delete")
    async def delete_build(commit_hash: str, request: Request):
        record = store.find_build_by_commit(commit_hash)
        if record and record.drive_file_id:
            oauth = store.get_oauth_config()
            if oauth.refresh_token:
                await drive_uploader.delete_file(
                    record.drive_file_id, oauth
                )
        await store.delete_build(commit_hash)
        return RedirectResponse(url="/builds?deleted=1", status_code=303)

    return router
