#!/usr/bin/env bash
# Pulls the `live` branch and rebuilds when it moves. Run every 2 minutes by the
# binjamin-update systemd timer (deploy/README.md, "Auto-deploy"). GitHub only moves `live`
# after the tests pass on main, so nothing has to log in to this server to deploy.
set -euo pipefail

# Run from a copy: the reset below rewrites this file while bash is still reading it.
if [ -z "${BINJAMIN_UPDATE_COPY:-}" ]; then
  cp "$0" /tmp/binjamin-autoupdate.sh
  BINJAMIN_UPDATE_COPY=1 exec bash /tmp/binjamin-autoupdate.sh "$@"
fi

cd "$HOME/binbot"
# One run at a time: a slow build must never overlap the next timer tick (two overlapping
# deploys once left Caddy created but never started, and the site was down).
exec 9>/tmp/binjamin-autoupdate.lock
flock -n 9 || exit 0

C="docker compose -f deploy/docker-compose.prod.yml"
ENV=deploy/.env.production
ALERTED=/tmp/binjamin-down-alerted

envval() { grep -E "^$1=" "$ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"'"; }

# A message to the team's alerts channel in Slack (SLACK_ALERTS_CHANNEL, with SLACK_ALERT_MENTION).
alert() {
  local token channel mention
  token=$(envval SLACK_BOT_TOKEN); channel=$(envval SLACK_ALERTS_CHANNEL); mention=$(envval SLACK_ALERT_MENTION)
  [ -n "$token" ] && [ -n "$channel" ] || return 0
  python3 - "$token" "$channel" "$mention $1" <<'PY' || true
import json, sys, urllib.request
token, channel, text = sys.argv[1:4]
req = urllib.request.Request("https://slack.com/api/chat.postMessage",
    data=json.dumps({"channel": channel, "text": text.strip(), "unfurl_links": False}).encode(),
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"})
urllib.request.urlopen(req, timeout=10)
PY
}

# Services that should be running but aren't (Created, Exited, missing).
down() {
  comm -23 <($C config --services | sort) <($C ps --services --status running | sort)
}

# Every container up and the site answering through Caddy. Checked on every run, deploy or
# not, so a container that stops for any reason is restarted within a couple of minutes.
check_all() {
  local missing domain
  missing=$(down)
  if [ -n "$missing" ]; then
    echo "not running: $(echo $missing); starting them" >&2
    $C up -d >/dev/null 2>&1 || true
    sleep 10
    missing=$(down)
  fi
  domain=$(envval DOMAIN)
  if [ -z "$missing" ] && { [ -z "$domain" ] || curl -fsS -o /dev/null --max-time 15 "https://$domain/api/health"; }; then
    if [ -f "$ALERTED" ]; then
      rm -f "$ALERTED"
      alert ":white_check_mark: binjamin.uk is back: every container is running and the site answers."
    fi
    return 0
  fi
  if [ ! -f "$ALERTED" ]; then  # once per outage, not every 2 minutes
    touch "$ALERTED"
    alert ":rotating_light: binjamin.uk needs a look: ${missing:+not running: $(echo $missing). }${domain:+$(curl -fsS -o /dev/null --max-time 15 "https://$domain/api/health" 2>/dev/null || echo "the site isn't answering. ")}Tried restarting once. On the server: docker ps -a, then docker compose -f deploy/docker-compose.prod.yml up -d"
  fi
  return 1
}

git fetch -q origin live
new=$(git rev-parse origin/live)
old=$(git rev-parse HEAD)
if [ "$new" = "$old" ]; then check_all || exit 1; exit 0; fi
# Forward only: if this server is already ahead of live (started by hand from a newer main),
# wait for live to catch up rather than going back to an older version.
if git merge-base --is-ancestor "$new" "$old"; then check_all || exit 1; exit 0; fi

healthy() {
  for _ in $(seq 1 30); do
    if $C exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" 2>/dev/null; then
      return 0
    fi
    sleep 3
  done
  return 1
}

echo "updating ${old:0:7} -> ${new:0:7}"
git reset -q --hard "$new"
$C up -d --build
if healthy; then
  # Pick up any Caddyfile change (it's mounted, so the container doesn't restart for it).
  $C exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
  echo "live on ${new:0:7}"
  docker image prune -f >/dev/null
  check_all || exit 1
  exit 0
fi

echo "the site didn't come back on ${new:0:7}; going back to ${old:0:7}" >&2
git reset -q --hard "$old"
$C up -d --build
healthy && echo "back on ${old:0:7}" >&2
alert ":warning: Deploy of ${new:0:7} didn't come up healthy, so the server went back to ${old:0:7}."
check_all || true
exit 1
