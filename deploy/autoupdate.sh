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
git fetch -q origin live
new=$(git rev-parse origin/live)
old=$(git rev-parse HEAD)
[ "$new" = "$old" ] && exit 0
# Forward only: if this server is already ahead of live (started by hand from a newer main),
# wait for live to catch up rather than going back to an older version.
if git merge-base --is-ancestor "$new" "$old"; then exit 0; fi

C="docker compose -f deploy/docker-compose.prod.yml"
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
  echo "live on ${new:0:7}"
  docker image prune -f >/dev/null
  exit 0
fi

echo "the site didn't come back on ${new:0:7}; going back to ${old:0:7}" >&2
git reset -q --hard "$old"
$C up -d --build
healthy && echo "back on ${old:0:7}" >&2
exit 1
