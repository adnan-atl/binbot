# Binjamin — context for Claude sessions

Binjamin (binjamin.uk, by Alfred) is a free WhatsApp assistant that messages UK residents the
evening before bin day with which bins to put out. Owner: Adnan (adnan@alfred.co.uk).
Note the spelling: binjamin.uk, not benjamin.uk (that's someone else's site).

## Stack and deploy
- FastAPI + SQLAlchemy + Postgres, Docker on AWS Lightsail (instance `binjamin`).
- Two processes: the API (`app/main.py`) and the worker (`app/workers/main.py`, APScheduler:
  dispatch every minute, `send_fallbacks` every 15s, nightly monitor/purge jobs).
- Deploy = `git push origin everything:main` from `~/binbot` (auto-deploys). Work happens on the
  local branch `everything`. Only Adnan pushes.
- Server config: `deploy/.env.production` on the server (never commit it; example file in repo).
- Tests: `python -m pytest -q` (must stay green before every commit).

## Where things live
- `app/services/reminders.py` — all WhatsApp copy and inbound handling (`handle_inbound`),
  settings menus, share messages, not-understood fallbacks.
- `app/services/wa_signup.py` — sign-up inside WhatsApp. `app/services/inbox.py` — team Slack inbox.
- `app/services/normalize.py` + `app/data/council_colours.json` — bin colours per council
  (341 of 343 checked; Google Sheet "Binjamin – council bin colours" is the working copy).
- `web/designs/oat.html` — the main website (HOME_DESIGN=oat). `web/designs/og.html` must stay
  identical to `silly.html` (the original page, kept at /d/og). Don't edit silly/og.
- `docs/` — inbox playbook, WhatsApp templates.

## Product rules (don't break these)
- Never guess a bin colour. Only use the council's own words; if unsure, leave it blank.
  Colours come from: the bin's label, then residents' picks, then council_colours.json.
  Where a council uses a few colours ("black or grey"), `options` limits the picker to those.
- Bot messages say "the evening before"; marketing copy may say "the night before".
- Not-understood messages: no instant reply. Wait for 60s of quiet, answer the burst once, at
  most once per 24h; everything still goes to the team inbox.
- British English. Brand name comes from settings (`brand_name`), never hard-coded in copy.

## Working conventions
- Commit messages end with the Co-Authored-By / Claude-Session trailers the session gives you.
- Don't sweep someone else's uncommitted changes into a commit; stage only your files.
- Never commit secrets or `.env` files; never print keys.
- Running several sessions at once: give each its own branch or git worktree
  (`git worktree add ../binbot-<task> -b <task>`), so they don't edit the same files.
  Merge into `everything` when done, then push.
- Show screenshots for UI changes (phone and desktop).
