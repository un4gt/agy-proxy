"""Shared React entry point, built by Vite and served locally."""
from pathlib import Path
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).parent / "static"

def ui_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})
