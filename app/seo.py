"""
Search and AI-search foundations for the public site (Dimitris's principles, applied to
Binjamin): every public URL gets its own title, description and canonical; structured data says
what Binjamin is and who makes it; robots.txt and sitemap.xml tell crawlers what to read; and
llms.txt gives AI assistants a plain summary built from the site's own words.

The pages are server-rendered HTML already, so crawlers and LLMs see the content without a
pre-render step. One HTML file serves /, /about and /faqs (the screens switch in the browser),
so the per-route head below is what keeps them from looking like duplicates.
"""
import html as _html
import json
import re
from pathlib import Path

from app.core.config import get_settings

# Public pages: path -> (title, description). {brand} and {parent} are filled in.
PAGES: dict[str, tuple[str, str]] = {
    "/": ("{brand} – free bin day reminders on WhatsApp",
          "{brand} is a free WhatsApp assistant that reminds you which bin to put out the night "
          "before collection. Works with 300+ UK councils."),
    "/about": ("About {brand} – bin day reminders by {parent}",
               "{brand} is a free WhatsApp assistant made by {parent}. It reads your council's own "
               "collection calendar, keeps checking it for changes, and messages you the night "
               "before each collection."),
    "/faqs": ("{brand} FAQs – how WhatsApp bin day reminders work",
              "How {brand} knows your bin days, which councils it covers, what it costs (nothing), "
              "changing your reminders, stopping them, and what happens to your data."),
    "/privacy": ("Privacy – {brand}", "How {brand} uses your address and WhatsApp number."),
    "/terms": ("Terms – {brand}", "The terms for using {brand}."),
}


def _fill(text: str) -> str:
    s = get_settings()
    return text.format(brand=s.brand_name, parent=s.parent_name)


def _base() -> str:
    return get_settings().public_base_url.rstrip("/")


def faqs_from(page: str) -> list[tuple[str, str]]:
    """The FAQ questions and answers as written on the page (<details class="faq">), so the
    structured data and llms.txt never drift from what visitors read."""
    out = []
    for m in re.finditer(r'<details class="faq"><summary>(.*?)<span[^>]*></span></summary>'
                         r'<div class="faq-a"><div>(.*?)</div></div></details>', page, re.S):
        q, a = (re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
                for x in m.groups())
        if q and a:
            out.append((q, re.sub(r"\s+([.,;:!?])", r"\1", a)))
    return out


def _org_id() -> str:
    """Alfred's @id, the same one alfred.co.uk's own structured data uses, so search engines see
    one Alfred rather than one per site."""
    return get_settings().parent_url.rstrip("/") + "/#organization"


def structured_data(path: str, page: str, modified: str | None = None) -> str:
    """JSON-LD, one linked graph: who makes it (Alfred), the site, the service itself (free,
    UK-wide, on WhatsApp), this page (with its author and when it last changed), and on /faqs
    the FAQ page. Entities point at each other by @id instead of being repeated."""
    s, base = get_settings(), _base()
    url = base + ("/" if path == "/" else path)
    org = {"@id": _org_id()}
    webpage = {"@type": "FAQPage" if path == "/faqs" else "AboutPage" if path == "/about" else "WebPage",
               "@id": f"{url}#webpage", "url": url, "name": _fill(PAGES[path][0]),
               "description": _fill(PAGES[path][1]), "inLanguage": "en-GB",
               "isPartOf": {"@id": f"{base}/#website"}, "about": {"@id": f"{base}/#service"},
               "author": org, "publisher": org}
    if modified:
        webpage["dateModified"] = modified
    if path == "/faqs":
        webpage["mainEntity"] = [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faqs_from(page)]
    graph = [
        {"@type": "Organization", "@id": _org_id(), "name": s.parent_name, "url": s.parent_url},
        {"@type": "WebSite", "@id": f"{base}/#website", "url": f"{base}/", "name": s.brand_name,
         "inLanguage": "en-GB", "publisher": org},
        {"@type": "Service", "@id": f"{base}/#service", "name": s.brand_name,
         "serviceType": "Bin collection reminders", "description": _fill(PAGES["/"][1]),
         "provider": org, "areaServed": {"@type": "Country", "name": "United Kingdom"},
         "availableChannel": {"@type": "ServiceChannel", "name": "WhatsApp", "serviceUrl": f"{base}/"},
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "GBP"}},
        webpage,
    ]
    data = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)
    return '<script type="application/ld+json">' + data.replace("</", "<\\/") + "</script>"


def apply(page: str, path: str, modified: str | None = None) -> str:
    """Give this public URL its own head: title, description, canonical, og title/description/
    url, author, and the structured data (with when the page last changed, ISO 8601 with
    offset). Pages that aren't public (sign-up steps, designs) are left alone. Runs after the
    {{BRAND}} tokens are filled in."""
    if path not in PAGES:
        return page
    title, desc = (_html.escape(_fill(x)) for x in PAGES[path])
    url = _base() + ("/" if path == "/" else path)
    page = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", page, count=1, flags=re.S)
    page = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{desc}">', page, count=1)
    page = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{title}">', page, count=1)
    page = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{desc}">', page, count=1)
    page = re.sub(r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{url}">', page, count=1)
    page = page.replace('<html lang="en">', '<html lang="en-GB">', 1)
    head = (f'<link rel="canonical" href="{url}">\n'
            f'<meta name="author" content="{_html.escape(get_settings().parent_name)}">\n'
            f'{structured_data(path, page, modified)}\n')
    return page.replace("</head>", head + "</head>", 1)


# AI crawlers and assistants, each given its own explicit allow group (Dimitris: an explicit
# group per bot is clearer to them than relying on "*", and some only obey their own group).
AI_BOTS = ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User", "Claude-SearchBot",
           "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot", "Applebot-Extended",
           "Amazonbot", "meta-externalagent", "CCBot")
_NOT_PAGES = ("/api/", "/webhooks/", "/r/", "/s/", "/w/", "/d/")


def robots_txt() -> str:
    """Everything public is open; the API, webhooks, share links and design previews aren't
    pages. AI crawlers are welcome: being found by them is the point. Test copies of the site
    are kept out of search with noindex (see is_canonical_host), not here."""
    group = ["Allow: /"] + [f"Disallow: {p}" for p in _NOT_PAGES]
    lines = ["User-agent: *", *group, ""]
    for bot in AI_BOTS:
        lines += [f"User-agent: {bot}", *group, ""]
    return "\n".join(lines + [f"Sitemap: {_base()}/sitemap.xml", ""])


def is_canonical_host(host: str | None) -> bool:
    """True when the page is served on the public address (binjamin.uk). Anything else (the
    server's IP, a staging name) gets noindex so it never competes with the real site."""
    from urllib.parse import urlsplit

    return (host or "").lower() == (urlsplit(_base()).hostname or "").lower()


def sitemap_xml(lastmod: str) -> str:
    urls = "".join(
        f"<url><loc>{_base()}{'/' if p == '/' else p}</loc><lastmod>{lastmod}</lastmod>"
        f"<priority>{'1.0' if p == '/' else '0.8' if p in ('/about', '/faqs') else '0.3'}</priority></url>"
        for p in PAGES)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n')


def llms_txt(page: str) -> str:
    """https://llmstxt.org: a plain-language summary for AI assistants. The facts come from the
    site's own copy (PAGES, the FAQs as written on the page), so it can't drift from what
    visitors read. Sections follow what people ask; the Optional section is the part an
    assistant short of context can skip. Served as text/plain; charset=utf-8."""
    s, base = get_settings(), _base()
    faqs = faqs_from(page)
    lines = [
        f"# {s.brand_name}",
        "",
        f"> {_fill(PAGES['/'][1])} Made by {s.parent_name}.",
        "",
        f"{s.brand_name} is a free bin collection reminder service for UK households, delivered on "
        f"WhatsApp. It is made and run by {s.parent_name} ({s.parent_url}). There is no app to "
        "install and no charge.",
        "",
        "## What it does",
        "",
        "- Sends a WhatsApp message the night before each bin collection, saying which bins go out.",
        "- Reads the resident's council's own collection calendar, and keeps checking it for changes "
        "such as bank holidays.",
        "- Works with 300+ UK councils. If a council isn't covered, residents can add their bin days "
        "themselves and still get reminders.",
        "",
        "## How to start",
        "",
        f"1. Go to {base}/ and enter your postcode, then pick your address.",
        "2. Check your bins and collection days.",
        f"3. Enter your WhatsApp number and choose a reminder time, then say hi to {s.brand_name} on "
        "WhatsApp.",
        "",
        "## Pages",
        "",
        f"- [Sign up]({base}/): start here; postcode lookup, bin check and reminder set-up",
        f"- [About {s.brand_name}]({base}/about): who makes it and how it gets your bin days",
        f"- [Questions and answers]({base}/faqs): coverage, cost, changing or stopping reminders, data",
        "",
    ]
    if faqs:
        lines += ["## Common questions", ""]
        for q, a in faqs:
            lines += [f"### {q}", "", a, ""]
    lines += [
        "## Optional",
        "",
        f"- [Privacy]({base}/privacy): what is stored (address, WhatsApp number) and why",
        f"- [Terms]({base}/terms): the terms of use",
        f"- [{s.parent_name}]({s.parent_url}): the company behind {s.brand_name}",
        "",
    ]
    return "\n".join(lines)


def lastmod_iso(path: Path) -> str:
    """When a page file last changed, ISO 8601 with the UK offset (for dateModified)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(int(path.stat().st_mtime), ZoneInfo("Europe/London")).isoformat()


def lastmod_of(path: Path) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()
