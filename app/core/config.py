from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+psycopg://binbot:binbot@localhost:5432/binbot"

    # Demo mode: no outbound calls; canned postcode/address/council data.
    demo_mode: bool = False

    # Geo + address lookup
    postcodes_io_url: str = "https://api.postcodes.io"
    address_provider: str = "none"  # none | os_places | ideal_postcodes
    # Tried when the main provider errors or returns nothing for a postcode.
    address_fallback: str = "ideal_postcodes"  # none | os_places | ideal_postcodes
    os_places_api_key: str = ""
    ideal_postcodes_api_key: str = ""

    # Council scraping
    scrape_timeout_seconds: int = 90
    browser_sessions: int = 4  # must match SE_NODE_MAX_SESSIONS in docker-compose.yml
    selenium_url: str = ""  # e.g. http://selenium:4444 for councils that need a browser
    refresh_interval_days: int = 7
    refresh_window_days_before_holiday: int = 21
    horizon_days: int = 90  # how far ahead we keep events

    # Messaging
    message_provider: str = "stub"  # stub | twilio | meta
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""  # whatsapp:+14155238886
    meta_token: str = ""
    meta_phone_number_id: str = ""

    public_base_url: str = "http://localhost:8000"
    timezone: str = "Europe/London"
