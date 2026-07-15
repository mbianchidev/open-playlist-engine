"""FastAPI application entry point.

Importing :mod:`app.providers` registers the bundled provider adapters.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.providers  # noqa: F401  (registers adapters on import)
from app import __version__
from app.api import auth, migrations, owner_session, playlists, providers, shares
from app.core.logging import configure_share_token_redaction
from app.settings import get_settings

configure_share_token_redaction()
settings = get_settings()
app = FastAPI(title="Open Playlist Engine", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(
        {
            "http://localhost:5173",
            settings.frontend_url.rstrip("/"),
            settings.public_base_url_normalized,
        }
        - {""}
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(providers.router)
app.include_router(owner_session.router)
app.include_router(auth.router)
app.include_router(playlists.router)
app.include_router(migrations.router)
app.include_router(shares.router)
app.include_router(shares.public_router)
app.include_router(shares.page_router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "version": __version__, "mode": get_settings().deployment_mode.value}
