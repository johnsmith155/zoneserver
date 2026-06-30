#!/usr/bin/env bash
#
# ZoneVPN one-shot installer for a Debian/Ubuntu server (the "Iran server").
# It installs dependencies, downloads xray-core + a GeoIP database, runs the
# interactive setup wizard, and installs a systemd service that keeps the
# collector running and restarts it on boot/crash.
#
# Usage:   sudo bash install.sh
#
set -euo pipefail

# --------------------------------------------------------------------------- #
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="zonevpn"
DASH_NAME="zonevpn-dashboard"
RUN_USER="${SUDO_USER:-$(whoami)}"

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
err() { printf "\n\033[1;31m!! %s\033[0m\n" "$*" >&2; }

if [[ "$EUID" -ne 0 ]]; then
  err "Please run with sudo:  sudo bash install.sh"
  exit 1
fi

# --------------------------------------------------------------------------- #
say "Installing system packages (python3, venv, unzip, curl) ..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip unzip curl ca-certificates

# --------------------------------------------------------------------------- #
say "Creating Python virtual environment ..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# --------------------------------------------------------------------------- #
# Pinned to v25.12.8 - the LAST xray release that still supports the TLS
# 'allowInsecure' option (removed from v26.1.x onward). It parses all modern
# config fields AND lets the many free trojan/vmess/tls configs with a fake SNI
# pass testing -> much higher yield. Override with:
#   XRAY_VERSION=latest sudo bash install.sh
# (then set tls_allow_insecure=false in config.json, else newer xray won't start).
XRAY_VERSION="${XRAY_VERSION:-v25.12.8}"
say "Downloading xray-core ${XRAY_VERSION} ..."
mkdir -p "$APP_DIR/bin"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)  XPKG="Xray-linux-64.zip" ;;
  aarch64|arm64) XPKG="Xray-linux-arm64-v8a.zip" ;;
  armv7l)        XPKG="Xray-linux-arm32-v7a.zip" ;;
  *) err "Unsupported architecture: $ARCH"; exit 1 ;;
esac
if [ "$XRAY_VERSION" = "latest" ]; then
  XURL="https://github.com/XTLS/Xray-core/releases/latest/download/${XPKG}"
else
  XURL="https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${XPKG}"
fi
TMP="$(mktemp -d)"
curl -fsSL "$XURL" -o "$TMP/xray.zip"
unzip -o "$TMP/xray.zip" xray -d "$APP_DIR/bin" >/dev/null
chmod +x "$APP_DIR/bin/xray"
rm -rf "$TMP"
"$APP_DIR/bin/xray" version | head -n1 || true

# --------------------------------------------------------------------------- #
say "Downloading GeoIP (country) database ..."
# Loyalsoldier/geoip ships a MaxMind-format Country.mmdb with no license key.
if curl -fsSL "https://github.com/Loyalsoldier/geoip/releases/latest/download/Country.mmdb" \
     -o "$APP_DIR/GeoLite2-Country.mmdb"; then
  echo "GeoIP database installed."
else
  err "GeoIP download failed - the app will fall back to ip-api.com (still works)."
fi

# --------------------------------------------------------------------------- #
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

# --------------------------------------------------------------------------- #
if [[ ! -f "$APP_DIR/config.json" ]]; then
  say "Running the setup wizard (you'll be asked for your GitHub token & gist) ..."
  sudo -u "$RUN_USER" "$APP_DIR/venv/bin/python" "$APP_DIR/setup_wizard.py"
else
  say "config.json already exists - skipping the wizard."
  echo "    (re-run later with: $APP_DIR/venv/bin/python setup_wizard.py)"
fi

# --------------------------------------------------------------------------- #
# Make sure the dashboard has a token (generate one if missing) and ensure the
# dashboard_* keys exist in config.json regardless of how it was created.
say "Configuring the dashboard ..."
DASH_TOKEN="$("$APP_DIR/venv/bin/python" - "$APP_DIR/config.json" <<'PY'
import json, secrets, sys
p = sys.argv[1]
try:
    with open(p, encoding="utf-8") as fh: cfg = json.load(fh)
except Exception: cfg = {}
cfg.setdefault("dashboard_host", "0.0.0.0")
cfg.setdefault("dashboard_port", 8787)
if not cfg.get("dashboard_token"):
    cfg["dashboard_token"] = secrets.token_urlsafe(18)
with open(p, "w", encoding="utf-8") as fh: json.dump(cfg, fh, indent=2, ensure_ascii=False)
print(cfg["dashboard_token"])
PY
)"
chown "$RUN_USER":"$RUN_USER" "$APP_DIR/config.json"
chmod 600 "$APP_DIR/config.json"
mkdir -p "$APP_DIR/state"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/state"

# Allow the (non-root) dashboard user to run the hard-update script via sudo
# without a password — scoped to exactly that one script.
say "Installing sudoers rule for the Update button ..."
cat > "/etc/sudoers.d/zonevpn" <<EOF
${RUN_USER} ALL=(root) NOPASSWD: ${APP_DIR}/update.sh
EOF
chmod 440 "/etc/sudoers.d/zonevpn"
chmod +x "$APP_DIR/update.sh"

# --------------------------------------------------------------------------- #
say "Installing systemd service ..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=ZoneVPN free V2Ray config collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python -m zonevpn
Restart=always
RestartSec=15
# light footprint / sane limits
Nice=10
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

say "Installing dashboard systemd service ..."
cat > "/etc/systemd/system/${DASH_NAME}.service" <<EOF
[Unit]
Description=ZoneVPN server dashboard (logs / servers / update)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python -m zonevpn.dashboard
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" "${DASH_NAME}"
systemctl restart "${SERVICE_NAME}"
systemctl restart "${DASH_NAME}"

# Best-effort: open the dashboard port if ufw is active.
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  DASH_PORT="$("$APP_DIR/venv/bin/python" -c "import json;print(json.load(open('$APP_DIR/config.json')).get('dashboard_port',8787))" 2>/dev/null || echo 8787)"
  ufw allow "${DASH_PORT}/tcp" >/dev/null 2>&1 || true
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
DASH_PORT="$("$APP_DIR/venv/bin/python" -c "import json;print(json.load(open('$APP_DIR/config.json')).get('dashboard_port',8787))" 2>/dev/null || echo 8787)"

say "Done!"
echo "  Collector status :  systemctl status ${SERVICE_NAME}"
echo "  Dashboard status :  systemctl status ${DASH_NAME}"
echo "  Logs             :  journalctl -u ${SERVICE_NAME} -f"
echo "  Edit config      :  ${APP_DIR}/venv/bin/python setup_wizard.py   (then: systemctl restart ${SERVICE_NAME})"
echo ""
echo "  ┌─ Dashboard ────────────────────────────────────────────────"
echo "  │  http://${IP:-SERVER_IP}:${DASH_PORT}/?token=${DASH_TOKEN}"
echo "  │  (token is saved in config.json as dashboard_token)"
echo "  └────────────────────────────────────────────────────────────"
