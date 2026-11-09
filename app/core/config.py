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
    os_places_per_minute: int = 45  # OS allows 50/min on our plan; keep a margin
    ideal_postcodes_api_key: str = ""

    # Council scraping
    scrape_timeout_seconds: int = 90
    # A UK proxy (http://user:pass@host:port) for councils whose sites don't answer cloud
    # servers (Glasgow). Only the scraper container has it; used only after a direct try fails.
    council_proxy_url: str = ""
    browser_sessions: int = 4  # must match SE_NODE_MAX_SESSIONS in docker-compose.yml
    selenium_url: str = ""  # e.g. http://selenium:4444 for councils that need a browser
    # Production: council scrapers run in their own container with no database or secrets
    # (app/scraper). Empty runs them in-process (local dev, tests).
    scraper_url: str = ""  # e.g. http://scraper:8100
    scraper_token: str = ""  # shared secret between api/worker and the scraper container
    refresh_interval_days: int = 7
    refresh_window_days_before_holiday: int = 21
    horizon_days: int = 90  # how far ahead we keep events
    # After a fresh council lookup, fetch this many neighbouring postcodes' calendars in the
    # background so the next sign-ups from the street load instantly (services/prefetch.py).
    # 0 = off. Off by default: each neighbour costs an OS Places lookup, and the OS trial is
    # capped at 2,000 lookups in total. Turn on (PREFETCH_NEIGHBOURS=6) once addresses come
    # from a source without that cap.
    prefetch_neighbours: int = 0
    # Ask people to share in the WhatsApp welcome? Off: people share once Binjamin has done
    # something for them (after their first Done, or the Sunday-evening nudge).
    share_in_welcome: bool = False
    # Splitting a postcode round moves residents and messages them. Until it's been watched
    # on real councils, only log what it would do ("would_split" in change_log).
    split_dry_run: bool = True

    # Messaging
    message_provider: str = "stub"  # stub | meta
    meta_token: str = ""
    meta_phone_number_id: str = ""
    meta_verify_token: str = ""  # any string you choose; paste the same one in Meta's webhook settings
    # App secret (Meta app dashboard -> App settings -> Basic). Every webhook POST is checked
    # against it (X-Hub-Signature-256). Empty skips the check: local testing only.
    meta_app_secret: str = ""
    # Approved message templates, for messages sent outside WhatsApp's 24-hour reply window
    # (reminders, date changes). Empty = send free text instead (fine for stub and testing).
    meta_reminder_template: str = "bin_reminder"
    meta_change_template: str = "bin_day_changed"
    # Optional: the reminder with a line of referral news in it (docs/whatsapp-templates.md).
    # Empty = the news goes in the reply to Done / Snooze instead.
    meta_reminder_news_template: str = ""
    # "Share with neighbours" nudge, the day after the first collection (docs/whatsapp-templates.md).
    # Empty until Meta approves it: until then only people who tap Done get the nudge (free text).
    meta_share_template: str = ""
    # "Your reminders are nearly ready" with a Start reminders button, for website sign-ups that
    # reached the say-hi step but didn't send the message within SIGNUP_NUDGE_MINUTES. Empty until
    # Meta approves it (docs/whatsapp-templates.md).
    meta_signup_nudge_template: str = ""
    meta_template_lang: str = "en_GB"
    # The business WhatsApp number in E.164 (+447...). When set, sign-up ends with the resident
    # sending us "hi" from their own WhatsApp, which proves the number is theirs and opens the
    # reply window for the welcome. Empty (local, stub): reminders start straight away.
    meta_display_number: str = ""
    # Team inbox in Slack (services/inbox.py): messages Binjamin can't answer are posted to the
    # channel; a ✅ on a reply in the thread sends it to the resident on WhatsApp. Empty token = off.
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_inbox_channel: str = "binjamin-inbox"  # name or ID; the Binjamin app must be in it
    slack_signups_channel: str = "binjamin-signups"  # every new sign-up is posted here; empty turns it off
    # The nightly growth report goes here at midnight (services/daily_report.py); empty turns it off
    slack_analytics_channel: str = "binjamin-analytics"
    # Slow or failed council lookups during sign-up are posted here, tagging these people
    # (Slack mentions, e.g. "<@U0C2FMB5RHD> <@U0123CLAUDE>"). Empty channel turns it off.
    slack_alerts_channel: str = "binjamin-inbox"
    slack_alert_mention: str = "<@U0C2FMB5RHD>"

    public_base_url: str = "http://localhost:8000"

    # Abuse protection on sign-up (address lookups are paid, council sites shouldn't be hammered).
    rate_limits: bool = True  # per-IP limits on the sign-up endpoints
    rate_limit_allow: str = ""  # IPs/ranges that skip the limits, e.g. "203.0.113.7,10.0.0.0/8"
    turnstile_site_key: str = ""  # Cloudflare Turnstile; both empty = no human check
    turnstile_secret: str = ""

    # Dead man's switch: the worker pings this URL after every reminder run (healthchecks.io,
    # Better Stack...), and the service alerts you when the pings stop. Empty = off.
    heartbeat_url: str = ""
    # Error alerts (sentry.io, EU region). Empty = off.
    sentry_dsn: str = ""
    # Product analytics (PostHog, EU cloud; services/analytics.py). The project API key
    # (phc_..., public by design: it can only send events). Empty = off, on the site and server.
    posthog_key: str = ""
    posthog_host: str = "https://eu.i.posthog.com"  # where the server sends its events
    # Where the website sends its events: our own address, proxied by Caddy to PostHog
    # (deploy/Caddyfile), so ad blockers don't drop them. Locally there's no Caddy: set this to
    # posthog_host to test the site's events.
    posthog_web_host: str = "/ingest"
    # Reading from PostHog, for the nightly report's visits and website funnel
    # (services/daily_report.py): a personal API key with read access to queries, and the
    # project's number (in its address: eu.posthog.com/project/<id>). Either empty = the report
    # goes out with the database numbers only.
    posthog_personal_api_key: str = ""
    posthog_project_id: str = ""
    # Product name shown on the page and in WhatsApp messages. Change it here or with BRAND_NAME.
    # Which page the home page (/) shows: a design in web/designs ("oat"; the original page is "og"), or empty for
    # web/index.html. Every design stays reachable at /d/<name>.
    home_design: str = ""
    brand_name: str = "Binjamin"
    # The company behind the product, and the links in the page footer and consent text.
    parent_name: str = "Alfred"
    parent_url: str = "https://alfred.co.uk"
    privacy_url: str = "https://alfred.co.uk/privacy"
    terms_url: str = "https://alfred.co.uk/legal"
    timezone: str = "Europe/London"


@lru_cache
def get_settings() -> Settings:
    return Settings()
