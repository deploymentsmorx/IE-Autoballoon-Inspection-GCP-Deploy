from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import auth, autoballoon, customers, users
from app.core.config import STATIC
from app.core.security import hash_password
from app.services import auth_service, autoballoon_service
from app.utils import storage_mode

app = FastAPI(title="SmorX AutoBalloon API", version="1.0.0")
DB_MODE = "unknown"

# Extra production frontend origins (comma-separated), set via env at deploy time
# so hosting domains don't require a code change/rebuild to allow.
_extra_origins = [
    o.strip() for o in os.getenv("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    # Explicit origins required when Authorization credentials are sent
    # (allow_origins=["*"] + allow_credentials=True breaks browser/WebView fetch).
    allow_origins=[
        "http://127.0.0.1:9835",
        "http://localhost:9835",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "https://tauri.localhost",
        "http://tauri.localhost",
        "https://localhost",
        "http://localhost",
        "https://autoballoon-web-827464408309.us-central1.run.app",
        *_extra_origins,
    ],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Required for AutoBalloon.exe (WebView2 https://tauri.localhost → http://127.0.0.1:9686)
    allow_private_network=True,
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(customers.router)
app.include_router(autoballoon.router)

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.on_event("startup")
def _startup():
    global DB_MODE
    try:
        from app.services.autoballoon_engine import _resolve_tessdata

        _resolve_tessdata()
    except Exception:
        pass
    DB_MODE = auth_service.startup(hash_password)


@app.get("/api/health")
def health():
    # Keep this instant — OCR/DB probes here made the EXE look "unreachable".
    return {
        "ok": True,
        "service": "smorx-autoballoon",
        "db": DB_MODE,
        "storage": storage_mode(),
    }
