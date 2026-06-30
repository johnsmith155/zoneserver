#!/usr/bin/env bash
#
# ZoneVPN auto-update checker.
#
# Fetches origin/<branch> and, ONLY if there are new commits, runs the hard
# update (update.sh) which syncs the code, migrates the config, and restarts the
# services. Driven by the `zonevpn-autoupdate.timer` systemd timer (hourly by
# default). A no-op when already up to date, so it's cheap to run often.
#
# Run by hand to force a check:   sudo bash autoupdate.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRANCH="${ZONEVPN_BRANCH:-main}"

cd "$APP_DIR"

git fetch --quiet origin "${BRANCH}" || { echo "fetch failed; skipping"; exit 0; }

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/${BRANCH}")"

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S')  already up to date (${LOCAL:0:9})"
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S')  new code ${LOCAL:0:9} -> ${REMOTE:0:9}; updating ..."
exec bash "$APP_DIR/update.sh"
