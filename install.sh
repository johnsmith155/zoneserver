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
say "Downloading xray-core ..."
mkdir -p "$APP_DIR/bin"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)  XPKG="Xray-linux-64.zip" ;;
  aarch64|arm64) XPKG="Xray-linux-arm64-v8a.zip" ;;
  armv7l)        XPKG="Xray-linux-arm32-v7a.zip" ;;
  *) err "Unsupported architecture: $ARCH"; exit 1 ;;
esac
XURL="https://github.com/XTLS/Xray-core/releases/latest/download/${XPKG}"
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

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

say "Done!"
echo "  Status :  systemctl status ${SERVICE_NAME}"
echo "  Logs   :  journalctl -u ${SERVICE_NAME} -f"
echo "  Edit   :  ${APP_DIR}/venv/bin/python setup_wizard.py   (then: systemctl restart ${SERVICE_NAME})"
