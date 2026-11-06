"""The production deploy files stay in step with the app's settings."""
from pathlib import Path

import pytest

from app.core.config import Settings

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _env_keys() -> set[str]:
    lines = (DEPLOY / ".env.production.example").read_text().splitlines()
    return {ln.split("=", 1)[0] for ln in lines if ln and not ln.startswith("#") and "=" in ln}


def test_env_example_lists_every_production_setting():
    keys = _env_keys()
    for k in ["BRAND_NAME", "PUBLIC_BASE_URL", "DOMAIN", "MESSAGE_PROVIDER", "META_TOKEN",
              "META_PHONE_NUMBER_ID", "META_VERIFY_TOKEN", "META_APP_SECRET", "META_DISPLAY_NUMBER",
              "META_REMINDER_TEMPLATE", "META_CHANGE_TEMPLATE", "META_TEMPLATE_LANG",
              "OS_PLACES_API_KEY", "IDEAL_POSTCODES_API_KEY", "PRIVACY_URL", "TERMS_URL",
              "DEMO_MODE", "SPLIT_DRY_RUN", "DATABASE_URL", "POSTGRES_PASSWORD"]:
        assert k in keys, k
    # Every app setting in the file is one the app actually reads
    known = {f.upper() for f in Settings.model_fields}
    compose_only = {"DOMAIN", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "BACKUP_S3_BUCKET",
                    "BACKUP_S3_REGION", "BACKUP_AWS_ACCESS_KEY_ID", "BACKUP_AWS_SECRET_ACCESS_KEY",
                    "BACKUP_HEARTBEAT_URL"}
    extra = keys - known - compose_only
    assert not extra, extra


def test_compose_has_the_services_and_volumes():
    yaml = pytest.importorskip("yaml")
    c = yaml.safe_load((DEPLOY / "docker-compose.prod.yml").read_text())
    assert set(c["services"]) == {"db", "api", "worker", "scraper", "selenium", "caddy", "backup", "offsite"}
    assert {"pgdata", "backups", "caddy_data"} <= set(c["volumes"])
    assert c["services"]["caddy"]["ports"] == ["80:80", "443:443"]
    assert "alembic upgrade head" in c["services"]["api"]["command"]
    assert "{$DOMAIN}" in (DEPLOY / "Caddyfile").read_text()


def test_secrets_stay_out_of_git_and_the_image():
    root = DEPLOY.parent
    assert "deploy/.env.production" in (root / ".gitignore").read_text().split()
    assert "deploy/.env.production" in (root / ".dockerignore").read_text().split()


def test_scrapers_are_locked_away_from_data_and_secrets():
    yaml = pytest.importorskip("yaml")
    c = yaml.safe_load((DEPLOY / "docker-compose.prod.yml").read_text())
    sv = c["services"]
    assert c["networks"]["data"]["internal"] is True
    assert sv["db"]["networks"] == ["data"]
    on_data = {n for n, v in sv.items() if "data" in (v.get("networks") or [])}
    assert on_data == {"db", "api", "worker", "backup"}  # not scraper, selenium or caddy
    scraper = sv["scraper"]
    # Never the main env file: at most its own .env.scraper, which holds only the proxy address
    assert [f["path"] if isinstance(f, dict) else f for f in scraper.get("env_file", [])] in ([], [".env.scraper"])
    assert "ports" not in scraper
    assert not any(k.startswith(("META", "DATABASE", "POSTGRES", "OS_PLACES", "IDEAL", "TURNSTILE"))
                   for k in scraper["environment"])
    assert scraper["read_only"] is True and scraper["cap_drop"] == ["ALL"]
    assert scraper["user"] != "0" and not scraper["user"].startswith("0:")
    for name in ("api", "worker"):
        assert sv[name]["environment"]["SCRAPER_URL"] == "http://scraper:8100"
        assert "SELENIUM_URL" not in sv[name]["environment"]  # only the scraper drives the browser


def test_offsite_copy_reads_backups_only():
    yaml = pytest.importorskip("yaml")
    c = yaml.safe_load((DEPLOY / "docker-compose.prod.yml").read_text())
    off = c["services"]["offsite"]
    assert off["volumes"] == ["backups:/backups:ro"]
    assert "data" not in (off.get("networks") or [])  # no route to the database
    assert ":latest" not in off["image"] and off["image"].count(":") == 1


def test_ready_checks_database_and_scraper(client, monkeypatch):
    import httpx

    from app.core.config import get_settings
    assert client.get("/api/ready").json() == {"ok": True}
    monkeypatch.setattr(get_settings(), "scraper_url", "http://scraper:8100")

    def down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", down)
    r = client.get("/api/ready")
    assert r.status_code == 503 and "scraper" in r.json()["detail"]
