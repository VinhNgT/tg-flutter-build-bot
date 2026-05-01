"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from ..drive.uploader import DriveUploader
from ..store import Store
from .routes import create_routes

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(store: Store, drive_uploader: DriveUploader) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Flutter Build Bot",
        description="Admin panel for the Telegram Flutter Build Bot",
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Include routes
    router = create_routes(store, drive_uploader, templates)
    app.include_router(router)

    return app
