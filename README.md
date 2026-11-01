# BinBot

Bin collection reminders on WhatsApp. Enter a postcode, BinBot finds your council's
collection calendar, you confirm the bins, and it messages you the night before.

## Run it

```bash
cp .env.example .env          # defaults work; keys only unlock more (see below)
docker compose up --build     # API on http://localhost:8000, worker alongside
```

Open http://localhost:8000 for the sign-up flow, http://localhost:8000/docs for the API.

Councils that need a real browser (~86 of 345) use the `selenium` service, which starts with everything else.

Check which councils work: `docker compose exec api python -m app.workers.sweep` (add a name to filter, `--no-browser` to skip browser councils). Results go to the `councils` table.

Without Docker:

```bash
pip install -e ".[dev]"
export DATABASE_URL=sqlite:///binbot.db   # or a Postgres URL
alembic upgrade head
uvicorn app.main:app --reload             # API + UI
python -m app.workers.main                # monitor + reminder dispatcher
pytest                                    # no network needed
```

`DEMO_MODE=true` makes any postcode resolve to canned Hackney data with no outbound
calls. Useful for UI work and CI.

## How it works

```
postcode ──► postcodes.io ──► council (GSS code) ──► adapter ──► Snapshot (raw)
                │                                                   │
                └─► address provider ──► UPRN                       ▼
                                                           CollectionEvent  (dates: source of truth)
                                                                    │
                                              + UserBinSetting      │  (enable/disable, manual rules)
                                                                    ▼
                                                               Reminder ──► WhatsApp
```

**Stage 1: lookup** (`app/services/schedule.py`)

1. `postcodes.io` turns a postcode into a local-authority GSS code and lat/lng.
2. `app/adapters/registry.py` maps the GSS code to a scraper in
   [UKBinCollectionData](https://github.com/robbrad/UKBinCollectionData) (330 councils
   as of this version). `pip install -U uk-bin-collection` picks up new ones.
3. Councils look addresses up by **UPRN**, not postcode. Configure `ADDRESS_PROVIDER`
   (OS Places; Ideal Postcodes as `ADDRESS_FALLBACK`) to offer a house picker. Without one, councils that
   need a UPRN (218 of 330) fall back to manual entry.
   **Address lookups are paid, so they happen once per postcode, not per user.** The
   default round key is `pc:<council>:<postcode>`: the first neighbour's lookup fetches
   the street's calendar, everyone else on that postcode reuses it (`source=neighbours`,
   no address lookup at all). A home whose dates differ hits "These aren't my days",
   which detaches it to its own round (`POST /users/{id}/own-calendar`). The monitor
   refreshes one representative property per round.
4. Dates from the council are stored as concrete `CollectionEvent`s. "Every other
   Tuesday" is derived for display only; the council's dates win, including bank
   holiday moves.
5. User edits live in `UserBinSetting` as a separate layer. They never overwrite
   council data, so a later refresh can't wipe them and they can't mask a real change.

**Stage 2: monitor** (`app/workers/monitor.py`)

| job            | when                        | what                                                                    |
| -------------- | --------------------------- | ----------------------------------------------------------------------- |
| `dispatch`     | every minute                | sends reminders that are due                                            |
| `refresh_stale`| hourly                      | re-fetches rounds older than 7 days (2 days in the 3 weeks before a bank holiday), diffs, rebuilds reminders, messages users whose dates moved |
| `health_check` | daily 04:15                 | probes every council we serve; flips status to `broken` after 3 failures, logs `adapter_broken` / `adapter_recovered` in `change_log` |
| `resolve_disputes` | daily 04:45            | probes rounds neighbours contradicted; splits a postcode that's really two rounds (odd/even, two ends) |

A broken adapter never silences anyone: reminders keep running on the last good snapshot.

## Data model

| table               | holds                                                               |
| ------------------- | ------------------------------------------------------------------- |
| `councils`          | one per local authority: adapter, what it needs, health status      |
| `bin_types`         | the council's bin labels, mapped to `recycling/general/food/garden/glass/paper/other` |
| `properties`        | UPRN, postcode, house number, `round_key` (cache key for the calendar) |
| `snapshots`         | every fetch, raw, with a content hash                               |
| `round_splits`      | postcodes that are two rounds, and the rule (parity or a cut) that says which side a home is on |
| `collection_events` | one row per bin per date; `moved_from` when a date shifted         |
| `users`             | phone (E.164), property, reminder time/mode, status, referral code  |
| `user_bin_settings` | per-user enable/disable and manual rules                            |
| `reminders`         | what to send, when, and what happened                               |
| `outbox`            | every message sent, whatever the provider                           |
| `change_log`        | diffs the monitor found, and adapter breakage                       |
| `referrals`         | who invited whom; levels in `app/models.LEVELS`                     |

## API (one call per screen)

```
GET  /api/postcode/{pc}                 council, support status, round_known; ?addresses=true runs the paid lookup
POST /api/signup/start                  creates user + property, reuses the postcode round or fetches → bins
POST /api/users/{id}/own-calendar       "not my days": own round + fresh fetch
GET  /api/users/{id}/bins
PUT  /api/users/{id}/bins               enable/disable, manual rules, add/remove
POST /api/users/{id}/phone              WhatsApp number + consent
POST /api/users/{id}/reminders          mode + time; turns reminders on
GET  /api/users/{id}/schedule
GET  /api/users/{id}/referral           code, link, level
POST /api/inbound                       DONE / SNOOZE / STOP (WhatsApp webhook: /webhooks/meta)
GET  /api/health                        adapter coverage
```

## WhatsApp

`MESSAGE_PROVIDER=stub` (default) writes every message to the `outbox` table and the
log, so the whole flow runs without credentials. `meta` sends through the WhatsApp
Cloud API: create an app at developers.facebook.com with the WhatsApp use case and its
free test number messages up to five verified recipients straight away. Free-form text
only goes through within 24 hours of the resident last messaging you; reminders outside
that window need approved message templates. Replies only reach `/webhooks/meta` once
the Meta app is published.

## Known gaps

- Address lookup needs an API key; there is no free UPRN source.
- Scrapers are only as good as UKBinCollectionData's; expect a few to break each month.
  The health check tells you which.
- Postcode rounds assume one calendar per postcode. The verify step checks it: a round is
  `confirmed` once a second address on the postcode returns the same dates
  (`app/services/quorum.py`), and `disputed` when the neighbours contradict it. Disputes and
  outvoted neighbours go to `change_log` (`disputed`, `neighbour_differs`) for ops; residents
  aren't shown them. Who gets asked rotates weekly, residents on the round first, so every
  home confirms the round over a few weeks, not the same two. A daily job probes disputed
  rounds (`app/services/split.py`): when exactly two calendars show up, each backed by two
  or more homes, and parity or one cut along the street explains them, the postcode is split
  (`round_splits`), residents on the other side are moved and told, and new sign-ups land
  on their side. Anything less certain is logged as `split_checked` for ops.
  Splits start in dry run (`SPLIT_DRY_RUN=true`): logged as `would_split` with who would move,
  nothing changed, until you've checked them on real councils and set it to `false`.
- Local fixes for broken upstream scrapers live in `app/adapters/overrides.py`.
- Bank holiday list in `monitor.py` is static; wire it to gov.uk/bank-holidays.json.
