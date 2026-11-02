"""The product name comes from one setting (BRAND_NAME): page and WhatsApp messages follow it."""
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Outbox


@pytest.fixture
def brand(monkeypatch):
    monkeypatch.setenv("BRAND_NAME", 'Bin & "Co"')
    get_settings.cache_clear()
    yield 'Bin & "Co"'
    monkeypatch.delenv("BRAND_NAME")
    get_settings.cache_clear()


def test_default_brand(client):
    page = client.get("/").text
    assert "<title>Binny " in page and "{{" not in page


def test_brand_name_setting(client, db, brand):
    uid = client.post("/api/signup/start", json={"postcode": "e83pb", "uprn": "10000002",
                                                 "house_number": "12"}).json()["user_id"]
    client.post(f"/api/users/{uid}/phone", json={"phone": "07700900123"})
    client.post(f"/api/users/{uid}/reminders", json={"mode": "night_before", "time": "19:00"})
    welcome = db.scalar(select(Outbox).where(Outbox.kind == "welcome"))
    assert welcome.body.startswith(f"{brand} here.")
    assert brand in client.get(f"/api/users/{uid}/referral").json()["share_text"]

    r = client.get("/")
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "<title>Bin &amp; &quot;Co&quot; –" in page  # escaped in HTML
    assert 'var BRAND = "Bin \\u0026 \\"Co\\"";' in page  # a safe JS string literal
    assert "{{" not in page and "Binny" not in page and "binbot.app" not in page
    assert client.get("/static/index.html").text == page


def test_unknown_number_gets_the_site_not_an_old_domain(client):
    reply = client.post("/api/inbound", json={"phone": "+447700900999", "text": "hi"}).json()["reply"]
    assert reply == "Hi! Sign up at localhost:8000 to get bin reminders."


def test_footer_links_and_parent_name(client, monkeypatch):
    page = client.get("/").text
    assert 'href="https://alfred.co.uk/privacy" target="_blank" rel="noopener">Privacy<' in page
    assert 'href="https://alfred.co.uk/legal" target="_blank" rel="noopener">Terms &amp; Conditions<' in page
    assert ">A product of Alfred</a>" in page and "Don't tell me about other Alfred services." in page
    assert page.count(">A product of Alfred</a>") == 1 and "Born in London" not in page
    assert "home-members" not in page and "Free for everyone. Included" not in page
    assert 'data-screen="about"' in page and 'data-screen="faqs"' in page
    assert '<a href="#about">' in page and '<a href="#faqs">' in page
    # About and FAQs carry the same brow, each marking itself as the current page
    assert '<a href="#about" aria-current="page">' in page and '<a href="#faqs" aria-current="page">' in page
    assert page.count('class="brow"') == 3 and page.count('class="page-card"') == 2

    monkeypatch.setenv("PARENT_NAME", "Fred & Co")
    monkeypatch.setenv("PARENT_URL", "https://fred.example/")
    monkeypatch.setenv("PRIVACY_URL", "https://fred.example/p?a=1&b=2")
    monkeypatch.setenv("TERMS_URL", 'https://fred.example/t"x')
    get_settings.cache_clear()
    try:
        page = client.get("/").text
    finally:
        for k in ("PARENT_NAME", "PARENT_URL", "PRIVACY_URL", "TERMS_URL"):
            monkeypatch.delenv(k)
        get_settings.cache_clear()
    assert page.count('<a href="https://fred.example/" target="_blank" rel="noopener">A product of Fred &amp; Co</a>') == 1
    assert page.count('href="https://fred.example/"') == 2  # footer and the FAQ "Who makes" answer
    assert "included with Fred &amp; Co membership." in page and "made by Fred &amp; Co, the" in page
    assert page.count('href="https://fred.example/p?a=1&amp;b=2"') == 3  # footer, consent text, FAQ
    assert page.count('href="https://fred.example/t&quot;x"') == 2
    assert "is made by Fred &amp; Co." in page and "{{" not in page
    assert "alfred.co.uk" not in page and "Alfred" not in page
