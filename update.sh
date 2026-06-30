#!/usr/bin/env bash
#
# ZoneVPN hard-update.
#
# Stops the collector, force-syncs the code to the latest origin/main (a HARD
# update — local code changes are discarded), reinstalls Python deps if they
# changed, then restarts both services. Your config.json, downloaded xray/GeoIP
# binaries and the state/ folder are git-ignored, so they are preserved and the
# collector simply resumes its cycle loop after the restart.
#
# Triggered by the dashboard "Update server" button (via sudo) or by hand:
#   sudo bash update.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="zonevpn"
DASH_NAME="zonevpn-dashboard"
BRANCH="${ZONEVPN_BRANCH:-main}"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }

cd "$APP_DIR"

say "Stopping ${SERVICE_NAME} ..."
systemctl stop "${SERVICE_NAME}" || true

say "Fetching latest code (branch: ${BRANCH}) ..."
git fetch --all --prune
PREV="$(git rev-parse HEAD)"
git reset --hard "origin/${BRANCH}"
NEW="$(git rev-parse HEAD)"
echo "    ${PREV:0:9} -> ${NEW:0:9}"

# Reinstall deps only if requirements.txt changed (cheap no-op otherwise).
if [ -x "$APP_DIR/venv/bin/pip" ]; then
  if ! git diff --quiet "${PREV}" "${NEW}" -- requirements.txt 2>/dev/null; then
    say "requirements.txt changed — reinstalling deps ..."
    "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
  else
    echo "    requirements unchanged — skipping pip install."
  fi
fi

# Self-heal the config: pull in any new settings/sections the new code expects,
# preserving the operator's existing values (token, gist id, tuned numbers).
if [ -x "$APP_DIR/venv/bin/python" ] && [ -f "$APP_DIR/config.json" ]; then
  say "Migrating config.json to the latest structure ..."
  "$APP_DIR/venv/bin/python" -m zonevpn.migrate || true
fi

# Refresh the `zonevpn` menu launcher so new menu features land automatically.
if [ -f "$APP_DIR/zonevpn.sh" ]; then
  chmod +x "$APP_DIR/zonevpn.sh"
  ln -sf "$APP_DIR/zonevpn.sh" /usr/local/bin/zonevpn 2>/dev/null || true
fi

say "Restarting services ..."
systemctl daemon-reload || true
systemctl start "${SERVICE_NAME}"
systemctl restart "${DASH_NAME}" 2>/dev/null || true

say "Update complete: ${NEW:0:9}"
