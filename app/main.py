import html
import json
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.api.webhooks import router as webhook_router
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=get_settings().brand_name, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(api_router)
app.include_router(webhook_router)

WEB = Path(__file__).resolve().parent.parent / "web"


def render_index() -> str:
    """The page with the brand filled in. Read on every request, so edits to web/ (mounted
    into the container) show on refresh. {{BRAND}} is for HTML text and attributes;
    {{BRAND_JSON}} is a JS string literal, safe inside <script>."""
    brand = get_settings().brand_name
    page = (WEB / "index.html").read_text(encoding="utf-8")
    brand_json = json.dumps(brand).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return page.replace("{{BRAND_JSON}}", brand_json).replace("{{BRAND}}", html.escape(brand))


if WEB.exists():
    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/static/index.html", include_in_schema=False, response_class=HTMLResponse)
    def index():
        return HTMLResponse(render_index())

    app.mount("/static", StaticFiles(directory=WEB), name="static")
