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
    # Splitting a postcode round moves residents and messages them. Until it's been watched
    # on real councils, only log what it would do ("would_split" in change_log).
    split_dry_run: bool = True

    # Messaging
    message_provider: str = "stub"  # stub | meta
    meta_token: str = ""
    meta_phone_number_id: str = ""
    meta_verify_token: str = ""  # any string you choose; paste the same one in Meta's webhook settings

    public_base_url: str = "http://localhost:8000"
    timezone: str = "Europe/London"


@lru_cache
def get_settings() -> Settings:
    return Settings()
