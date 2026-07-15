"""FastAPI application entry point.

Importing :mod:`app.providers` registers the bundled provider adapters.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.providers  # noqa: F401  (registers adapters on import)
from app import __version__
from app.api import auth, imports, migrations, playlists, providers
from app.settings import get_settings

app = FastAPI(title="Open Playlist Engine", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(providers.router)
app.include_router(auth.router)
app.include_router(playlists.router)
app.include_router(imports.router)
app.include_router(migrations.router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "version": __version__, "mode": get_settings().deployment_mode.value}
