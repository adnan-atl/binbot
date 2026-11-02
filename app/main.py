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
# httpx logs every request URL at INFO, query string included, and some providers take the API
# key in the URL (OS Places): keep those loggers to warnings and errors.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

app = FastAPI(title=get_settings().brand_name, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(api_router)
app.include_router(webhook_router)

WEB = Path(__file__).resolve().parent.parent / "web"


def render_index() -> str:
    """The page with the brand and links filled in. Read on every request, so edits to web/
    (mounted into the container) show on refresh. {{BRAND}}, {{PARENT_NAME}}, {{PARENT_URL}},
    {{PRIVACY_URL}} and {{TERMS_URL}} are for HTML text and attributes; {{BRAND_JSON}} is a JS
    string literal, safe inside <script>."""
    s = get_settings()
    page = (WEB / "index.html").read_text(encoding="utf-8")
    brand_json = json.dumps(s.brand_name).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = page.replace("{{BRAND_JSON}}", brand_json)
    for token, value in (("BRAND", s.brand_name), ("PARENT_NAME", s.parent_name),
                         ("PARENT_URL", s.parent_url), ("PRIVACY_URL", s.privacy_url),
                         ("TERMS_URL", s.terms_url)):
        page = page.replace("{{" + token + "}}", html.escape(value))
    return page


if WEB.exists():
    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/static/index.html", include_in_schema=False, response_class=HTMLResponse)
    def index():
        return HTMLResponse(render_index())

    app.mount("/static", StaticFiles(directory=WEB), name="static")
