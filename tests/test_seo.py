"""Search and AI-search foundations: per-URL heads, structured data, robots, sitemap, llms.txt."""
import json
import re

import pytest


@pytest.fixture
def silly(client, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("HOME_DESIGN", "silly")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://binjamin.uk")
    get_settings.cache_clear()
    yield client
    get_settings.cache_clear()


def _graph(page: str) -> list[dict]:
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))["@graph"]


@pytest.mark.parametrize("path", ["/", "/about", "/faqs", "/privacy", "/terms"])
def test_every_public_url_has_its_own_head(silly, path):
    r = silly.get(path)
    assert r.status_code == 200
    page = r.text
    url = "https://binjamin.uk" + path
    assert page.count("<title>") == 1 and page.count('name="description"') == 1
    assert f'<link rel="canonical" href="{url}">' in page
    assert f'<meta property="og:url" content="{url}">' in page
    assert '<html lang="en-GB">' in page
    assert "<meta name=\"author\" content=\"Alfred\">" in page
    graph = _graph(page)
    webpage = next(g for g in graph if g.get("@id", "").endswith("#webpage"))
    assert webpage["url"] == url
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", webpage["dateModified"])
    assert webpage["author"] == {"@id": "https://alfred.co.uk/#organization"}


def test_titles_differ_between_pages(silly):
    titles = {p: re.search(r"<title>(.*?)</title>", silly.get(p).text).group(1)
              for p in ("/", "/about", "/faqs")}
    assert len(set(titles.values())) == 3


def test_faq_schema_only_on_faqs_and_matches_the_page(silly):
    from app import seo
    faqs_page = silly.get("/faqs").text
    page_type = {g["@type"] for g in _graph(faqs_page)}
    assert "FAQPage" in page_type
    assert "FAQPage" not in {g["@type"] for g in _graph(silly.get("/").text)}
    faq = next(g for g in _graph(faqs_page) if g["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) == len(seo.faqs_from(faqs_page)) > 3


def test_site_nav_uses_real_links(silly):
    page = silly.get("/").text
    assert 'href="/about"' in page and 'href="/faqs"' in page
    assert 'href="#about"' not in page and 'href="#faqs"' not in page


def test_noindex_off_the_public_address(silly):
    assert "noindex" in silly.get("/").headers.get("x-robots-tag", "")  # testserver, not binjamin.uk
    r = silly.get("/", headers={"host": "binjamin.uk"})
    assert "x-robots-tag" not in r.headers


def test_robots_allows_ai_crawlers_explicitly(silly):
    r = silly.get("/robots.txt")
    body = r.text
    for bot in ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "PerplexityBot",
                "Google-Extended", "Applebot", "Amazonbot", "meta-externalagent", "CCBot"):
        assert f"User-agent: {bot}\nAllow: /\n" in body
    assert "Disallow: /api/" in body and "Disallow: /r/" in body
    assert "Sitemap: https://binjamin.uk/sitemap.xml" in body


def test_sitemap_lists_public_pages(silly):
    r = silly.get("/sitemap.xml")
    assert r.headers["content-type"].startswith("application/xml")
    locs = re.findall(r"<loc>(.*?)</loc>", r.text)
    assert locs == ["https://binjamin.uk/", "https://binjamin.uk/about", "https://binjamin.uk/faqs",
                    "https://binjamin.uk/privacy", "https://binjamin.uk/terms"]


def test_llms_txt_follows_the_spec(silly):
    r = silly.get("/llms.txt")
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    body = r.text
    assert re.match(r"# \w+\n\n> \S", body)
    assert "## Common questions" in body and "## Optional" in body
    assert body.index("## Optional") > body.index("## Common questions")
    assert "{{" not in body and "&#" not in body and "&amp;" not in body
