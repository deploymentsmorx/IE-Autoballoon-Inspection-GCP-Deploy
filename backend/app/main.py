from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import STATIC, hash_password
from . import store
from .routers import auth_admin, jobs

app = FastAPI(title="SmorX Inspection API", version="1.0.0")
DB_MODE = "unknown"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_admin.router)
app.include_router(jobs.router)

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.on_event("startup")
def _startup():
    global DB_MODE
    # Wire Tesseract for zoomed OCR fallback (PyMuPDF pdfocr / get_textpage_ocr)
    try:
        from .autoballoon import _resolve_tessdata
        _resolve_tessdata()
    except Exception:
        pass
    DB_MODE = store.startup(hash_password)


@app.get("/api/health")
def health():
    from .autoballoon import ocr_available
    return {
        "ok": True,
        "service": "smorx-inspection",
        "db": DB_MODE,
        "ocr": ocr_available(),
    }
