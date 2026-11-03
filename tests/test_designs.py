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
