# Product analytics (PostHog)

Binjamin sends product events to PostHog's EU cloud so we can see how people find it, where
sign-up loses them, and what they do with their reminders. It's off unless `POSTHOG_KEY` is set
in `deploy/.env.production`.

## How it's set up

- **Website** (`web/designs/oat.html` only; og/silly are untouched): PostHog's script, loaded
  through our own `/ingest` path. Caddy passes `/ingest` on to `eu.i.posthog.com`
  (`deploy/Caddyfile`), so ad blockers don't drop the events.
- **Server** (`app/services/analytics.py`): the official Python SDK, batched in a background
  thread in both the api and the worker.
- **One person, end to end**: once sign-up starts, the website calls `identify(<user id>)`, and
  every server event uses the same user id. A visit, the sign-up and the WhatsApp activity that
  follows line up as one person in PostHog.

## Privacy rules (don't break these)

- **Cookieless.** The website keeps PostHog's state in page memory only: no cookies, no
  localStorage, no banner needed. The trade-off: each page load is a new visitor, so "unique
  visitors" means visits, and there's no session replay.
- **No autocapture, no session recording, no heatmaps.** Only the named events below. Nothing a
  resident types (postcode, address, phone number, message text) is ever sent.
- **The distinct id is our random user id.** Never a phone number, postcode or address. Server
  properties are plain values and are run through the same scrubber as Sentry (`app/core/errors.py`).
- **Deleting an account** removes the user id from our database, so their PostHog events can no
  longer be tied back to anyone.
- The privacy page (`/privacy`) lists PostHog and describes the above. Change it if this changes.

## Events

Every website event carries `design: oat`. Every server event carries `source: server`,
`council` and `referred` (joined from someone's link).

### Website: visit and sign-up funnel

| Event | When | Properties |
|---|---|---|
| `$pageview` | Page load, and moving to /about, /faqs, /privacy, /terms | URL incl. `?ref=` and UTMs |
| `screen_viewed` | Every screen: home, postcode, parsing, confirm, phone, reminders, hi, binfluencer, about, faqs… | `screen` |
| `postcode_invalid` | Postcode didn't look like one | |
| `postcode_looked_up` | Council found for the postcode | `council`, `addresses` (count) |
| `postcode_lookup_failed` | Lookup errored | |
| `address_confirmed` | Address picked or house typed | `from_list`, `invite`, `own_lookup` |
| `calendar_loaded` / `calendar_failed` | Bin days back from the council (or not) | `council`, `bins`, `shared` |
| `signup_failed` | Sign-up call errored | |
| `not_my_days_tapped` | "These aren't my days" | |
| `bins_edit_opened`, `bins_edited` | Editing bins on the confirm step | `bins` |
| `calendar_confirmed` | "Yes, these are right" | `bins` |
| `phone_invalid`, `phone_added`, `phone_failed` | Phone step | `offers_ok` (didn't tick "Don't tell me about other services") |
| `say_hello_tapped` | "Say hello to Binjamin!" | `mode`, `time`, `on_computer` |
| `whatsapp_web_opened` | "Or open WhatsApp on this computer" (QR screen) | |
| `share_tapped` | Share step | `how`: whatsapp, copy_message, copy_link |

### Server: sign-up and WhatsApp

| Event | When | Properties |
|---|---|---|
| `signup_started` | Website address step, or first message from a new number | `via` (website / whatsapp), website: `calendar`, `shared`, `calendar_error`, `bins`; WhatsApp: `invite` |
| `reminders_switched_on` | The welcome goes out (sign-up finished) | `via`, `mode`, `time`, `bins` |
| `referral_credited` | A friend they invited switched on (on the referrer) | |
| `reminder_sent` | A reminder reached WhatsApp | `bins`, `mode`, `time`, `retried`, `with_news` |
| `reminder_failed` | Gave up on a reminder | |
| `reminder_replied` | DONE, SNOOZE or WRONG | `reply` |
| `menu_tapped` | A settings menu button | `item` |
| `reminder_time_changed` | New reminder time | `mode`, `time` |
| `bin_switched_on` / `bin_switched_off` | A bin turned on or off | `category` |
| `reminders_paused` / `reminders_resumed` | STOP / START | |
| `share_message_sent` | We sent the message to forward | `trigger` (asked, share, …) |
| `message_not_understood` | A message the bot couldn't handle (no text sent) | `pointed_elsewhere` |
| `asked_for_human` | "Talk to a human" | |
| `account_deleted` | DELETE YES | `via` |

## Useful insights to build

- **Sign-up funnel**: `$pageview` → `screen_viewed (postcode)` → `calendar_loaded` →
  `calendar_confirmed` → `phone_added` → `say_hello_tapped` → `reminders_switched_on`.
- **Activation by channel**: `reminders_switched_on` broken down by `via`.
- **Weekly active residents**: unique people with `reminder_replied` or `menu_tapped`.
- **Reminder engagement**: `reminder_replied` / `reminder_sent`, by `reply`.
- **Retention**: `reminders_switched_on` → `reminders_paused` / `account_deleted`.
- **Councils**: `calendar_failed` and `reminders_switched_on` by `council`.
- **Sharing**: `share_tapped` by `how`, and `referral_credited` over time.

## Testing locally

Set `POSTHOG_KEY` and `POSTHOG_WEB_HOST=https://eu.i.posthog.com` in `.env` (there's no Caddy
locally, so `/ingest` doesn't exist). PostHog drops events from headless browsers as bots.

## Nightly report (#binjamin-analytics)

At midnight (London) the worker posts the day just ended to `SLACK_ANALYTICS_CHANNEL`
(`app/services/daily_report.py`). Every headline number is compared with the same day last week.

- **Headline**: visits, sign-ups started, switched on (website / WhatsApp), visit → switched on,
  active residents, and people stuck at "say hello".
- **Website funnel**: unique visits at each step and the biggest drop.
- **Where visits came from**: invite links, UTMs, referring sites, direct.
- **Sharing and K-factor**: invite link clicks (`/r/<code>`), invited sign-ups and switch-ons,
  share messages and share taps, referrals credited. K-factor = invited switch-ons ÷ all
  switch-ons over 28 days (the steady-state viral coefficient: total = organic + K × total).
  Shown once there are 10 switch-ons to go on.
- **Engagement**: reminders sent and failed, reply rate (Done / Snooze / Wrong), pauses,
  deletions, settings changes, not understood, asked for a human.
- **Problem councils**: failed and slow (over 20s) lookups, bin days not found on the site,
  wrong-day votes, failed reminders, and councils whose health check is failing.

Sign-ups, invites, reminders and councils come from the database. Visits, the funnel, sources,
share taps, pauses and deletions come from PostHog and need `POSTHOG_PERSONAL_API_KEY` (scope
Query: read) and `POSTHOG_PROJECT_ID`; without them the report says so and sends the rest.
Preview any day on the server: `docker compose -f deploy/docker-compose.prod.yml exec worker
python -m app.services.daily_report 2026-11-07`.
