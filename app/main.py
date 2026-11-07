import html
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router as api_router
from app.api.webhooks import router as webhook_router
from app import seo
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO, query string included, and some providers take the API
# key in the URL (OS Places): keep those loggers to warnings and errors.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from app.core.errors import init_errors  # noqa: E402

init_errors("api")
app = FastAPI(title=get_settings().brand_name, version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(api_router)
app.include_router(webhook_router)

WEB = Path(__file__).resolve().parent.parent / "web"


PREVIEW_TAGS = """<meta name="description" content="{desc}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{{{{BRAND}}}}">
<meta property="og:title" content="{{{{BRAND}}}}: bin day reminders on WhatsApp">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{{{{PUBLIC_URL}}}}/">
<meta property="og:image" content="{{{{PUBLIC_URL}}}}/static/og.jpg?v=3">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="1200">
<meta property="og:image:alt" content="{{{{BRAND}}}}, a blue bin in a yellow hat and hi-vis vest, waving">
<meta name="twitter:card" content="summary">
""".format(desc="A free WhatsApp assistant that reminds you which bin to put out the night before "
                "collection. Works with 300+ UK councils.")


def render_index(page: Path | None = None) -> str:
    """The page with the brand and links filled in. Read on every request, so edits to web/
    (mounted into the container) show on refresh. {{BRAND}}, {{PARENT_NAME}}, {{PARENT_URL}},
    {{PRIVACY_URL}}, {{TERMS_URL}}, {{PUBLIC_URL}} and {{WA_HI_LINK}} are for HTML text and attributes; {{BRAND_JSON}} is a JS
    string literal, safe inside <script>."""
    s = get_settings()
    page = (page or WEB / "index.html").read_text(encoding="utf-8")
    if 'property="og:image"' not in page:  # every design gets the link preview (WhatsApp etc.)
        page = re.sub(r"(</title>\n?)", lambda m: m.group(1) + PREVIEW_TAGS, page, count=1)
    brand_json = json.dumps(s.brand_name).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = page.replace("{{BRAND_JSON}}", brand_json)
    page = page.replace("{{TURNSTILE_SITE_KEY_JSON}}", json.dumps(s.turnstile_site_key).replace("<", "\\u003c"))
    # Product analytics (services/analytics.py): only on designs that ask for it with this token
    # (oat), so the original pages stay as they were. null = off.
    ph = {"key": s.posthog_key, "host": s.posthog_web_host,
          "ui": s.posthog_host.replace(".i.posthog.com", ".posthog.com")} if s.posthog_key else None
    page = page.replace("{{POSTHOG_JSON}}", json.dumps(ph).replace("<", "\\u003c"))
    from app.services.reminders import start_link

    # The last step's WhatsApp link, with "Hi Binjamin, please start my bin reminders" ready to
    # send (same for everyone: no code in it). Empty without a business number (local, demo):
    # the page then turns reminders on directly.
    wa_hi = start_link() if s.meta_display_number else ""
    for token, value in (("WA_HI_LINK", wa_hi), ("PUBLIC_URL", s.public_base_url.rstrip("/")),
                         ("BRAND", s.brand_name), ("PARENT_NAME", s.parent_name),
                         ("PARENT_URL", s.parent_url), ("PRIVACY_URL", s.privacy_url),
                         ("TERMS_URL", s.terms_url)):
        page = page.replace("{{" + token + "}}", html.escape(value))
    return page


if WEB.exists():
    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False, response_class=HTMLResponse)
    @app.get("/static/index.html", include_in_schema=False, response_class=HTMLResponse)
    @app.api_route("/about", methods=["GET", "HEAD"], include_in_schema=False, response_class=HTMLResponse)
    @app.api_route("/faqs", methods=["GET", "HEAD"], include_in_schema=False, response_class=HTMLResponse)
    @app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False, response_class=HTMLResponse)
    @app.api_route("/terms", methods=["GET", "HEAD"], include_in_schema=False, response_class=HTMLResponse)
    def index(request: Request):
        path = request.url.path if request.url.path in seo.PAGES else "/"
        home = _home_page()
        # Off the public address (the server's IP, a test copy) the page stays out of search.
        headers = {} if seo.is_canonical_host(request.url.hostname) else {"X-Robots-Tag": "noindex"}
        return HTMLResponse(seo.apply(render_index(home), path, seo.lastmod_iso(home)), headers=headers)

    def _home_page() -> Path:
        """The page the public site serves: a design in web/designs (HOME_DESIGN), else index.html."""
        name = get_settings().home_design
        page = WEB / "designs" / f"{name}.html"
        return page if name and re.fullmatch(r"[a-z0-9-]{1,40}", name) and page.is_file() else WEB / "index.html"

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """Search engines and old browsers ask for /favicon.ico whatever the page links."""
        return FileResponse(WEB / "favicon.ico", media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
    def robots():
        return PlainTextResponse(seo.robots_txt())

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap():
        return Response(seo.sitemap_xml(seo.lastmod_of(_home_page())), media_type="application/xml")

    @app.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
    def llms():
        return PlainTextResponse(seo.llms_txt(render_index(_home_page())))

    @app.api_route("/r/{code}", methods=["GET", "HEAD"], include_in_schema=False)
    @app.get("/s/{code}", include_in_schema=False)  # old Share-button links
    @app.get("/w/{code}", include_in_schema=False)  # old WhatsApp invite links
    def referral_link(code: str, request: Request):
        """Someone's share link (binjamin.uk/r/<code>): counts the visit (ChangeLog "ref_visit" on
        whoever shared it), then opens the website with the referral saved, so their friend's
        sign-up is credited to them. Link-preview fetches (WhatsApp, Facebook) aren't counted."""
        from sqlalchemy import select

        from app.core.db import session_factory
        from app.models import ChangeLog, User

        if not re.fullmatch(r"[A-Za-z0-9_-]{4,12}", code):
            return RedirectResponse("/", status_code=302)
        agent = request.headers.get("user-agent", "")
        if not re.search(r"WhatsApp|facebookexternalhit|Facebot|bot|crawl|preview", agent, re.I):
            db = session_factory()()
            try:
                u = db.scalar(select(User).where(User.referral_code == code))
                if u is not None:
                    db.add(ChangeLog(scope="user", scope_id=u.id, kind="ref_visit", diff={}))
                    db.commit()
            except Exception:
                logging.getLogger(__name__).exception("referral visit not recorded")
                db.rollback()
            finally:
                db.close()
        return RedirectResponse(f"/?ref={code}", status_code=302)

    @app.get("/d/{name}", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/d/{name}/about", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/d/{name}/faqs", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/d/{name}/privacy", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/d/{name}/terms", include_in_schema=False, response_class=HTMLResponse)
    def design(name: str):
        """Design variants side by side: web/designs/<name>.html at /d/<name> (e.g. /d/silly)."""
        page = WEB / "designs" / f"{name}.html"
        if not re.fullmatch(r"[a-z0-9-]{1,40}", name) or not page.is_file():
            raise HTTPException(404, "No such design")
        # previews of the same site: never indexed, so they can't compete with the real pages
        return HTMLResponse(render_index(page), headers={"X-Robots-Tag": "noindex, nofollow"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.exception_handler(StarletteHTTPException)
async def not_found_page(request, exc):
    """Pages that don't exist get the 404 page (Binji's already collected it); the API and anything
    that isn't a browser asking for a page keep the usual JSON error."""
    wants_page = "text/html" in request.headers.get("accept", "")
    if exc.status_code == 404 and wants_page and not request.url.path.startswith(("/api", "/webhooks")):
        page = WEB / "404.html"
        if page.is_file():
            return HTMLResponse(render_index(page), status_code=404)
    return await http_exception_handler(request, exc)
