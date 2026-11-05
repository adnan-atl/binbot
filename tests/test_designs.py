"""Design variants are served side by side at /d/<name> from web/designs/<name>.html."""


def test_design_pages(client):
    r = client.get("/d/serious")
    assert r.status_code == 200 and "{{BRAND}}" not in r.text
    assert client.get("/d/silly").status_code == 200
    assert client.get("/d/alfred").status_code == 200
    assert client.get("/d/nope").status_code == 404
    assert client.get("/d/..%2Fapp").status_code == 404


def test_every_design_has_the_bot_check_wired_in(client):
    for name in ("serious", "silly", "alfred"):
        page = client.get(f"/d/{name}").text
        assert "{{" not in page, name  # every token filled in
        assert 'var TS_KEY = "";' in page and "X-Human-Pass" in page, name
        assert page.count("ensureHuman().then") == 2, name  # address lookup and sign-up


def test_added_bin_keeps_its_colour(client):
    from tests.test_flow import signup
    uid = signup(client)["user_id"]
    bins = client.get(f"/api/users/{uid}/bins").json()
    body = {"bins": [{"setting_id": b["setting_id"], "label": b["label"], "category": b["category"]} for b in bins]
            + [{"label": "Purple bin", "category": "other", "days": ["WED"], "frequency": "weekly", "colour": "purple"}]}
    out = client.put(f"/api/users/{uid}/bins", json=body).json()
    assert [b["colour"] for b in out if b["label"] == "Purple bin"] == ["purple"]
    body["bins"][-1]["colour"] = "tartan"
    assert client.put(f"/api/users/{uid}/bins", json=body).status_code == 400


def test_home_page_can_show_a_design(client, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv("HOME_DESIGN", "silly")
    get_settings.cache_clear()
    try:
        assert client.get("/").text == client.get("/d/silly").text
        monkeypatch.setenv("HOME_DESIGN", "nope")
        get_settings.cache_clear()
        assert client.get("/").status_code == 200  # unknown design falls back to index.html
    finally:
        monkeypatch.delenv("HOME_DESIGN")
        get_settings.cache_clear()


def test_about_and_faqs_have_their_own_addresses(client):
    for path in ("/about", "/faqs", "/privacy", "/terms", "/d/silly/about", "/d/silly/faqs", "/d/silly/terms"):
        assert client.get(path).status_code == 200, path
    assert client.get("/d/nope/about").status_code == 404


def test_missing_pages_get_the_404_page_and_the_api_keeps_json(client):
    r = client.get("/no-such-page", headers={"accept": "text/html"})
    assert r.status_code == 404 and "already been collected" in r.text and "Return to home page" in r.text
    r = client.get("/api/no-such-thing", headers={"accept": "text/html"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    assert client.get("/no-such-page").status_code == 404


def test_silly_time_step_says_hello_and_computers_get_the_qr_step(client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("HOME_DESIGN", "silly")
    monkeypatch.setenv("META_DISPLAY_NUMBER", "+44 7700 900999")
    get_settings.cache_clear()
    page = client.get("/").text
    assert "{{" not in page and "Scan with your phone" in page  # the QR step, for computers
    assert ('id="t-go" href="https://wa.me/447700900999?text=Hey%20Binny%2C%20I%27M%20BIN"' in page)
    assert 'aria-disabled="true"' in page and "Say hello to Binny!" in page
    monkeypatch.delenv("META_DISPLAY_NUMBER")
    get_settings.cache_clear()
    assert 'id="t-go" href=""' in client.get("/").text  # local: no WhatsApp link


def test_silly_phone_step_consent_wording(client, monkeypatch):
    """Wording agreed with legal: one notice, and the box is an opt-out."""
    from app.core.config import get_settings

    monkeypatch.setenv("HOME_DESIGN", "silly")
    get_settings.cache_clear()
    page = client.get("/").text
    assert ("Binny will message you on WhatsApp about your bins, as well as other household "
            "services within the Alfred family.") in page
    assert "Tick here to opt out of other Alfred household services" in page
    assert 'id="no-offers" type="checkbox"' in page
