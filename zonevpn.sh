#!/usr/bin/env bash
#
# ZoneVPN server control menu.
#
# Installed as /usr/local/bin/zonevpn — just run `zonevpn` on the server.
# One place to update, restart, watch logs, test a cycle, fix the config, and
# toggle automatic updates.
#
set -uo pipefail

# Resolve the real app dir even when invoked through the /usr/local/bin symlink.
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(cd "$(dirname "$SELF")" && pwd)"
PY="$APP_DIR/venv/bin/python"
SERVICE="zonevpn"
DASH="zonevpn-dashboard"
AUTO_TIMER="zonevpn-autoupdate.timer"

c_cyan='\033[1;36m'; c_grn='\033[1;32m'; c_red='\033[1;31m'; c_dim='\033[2m'; c_off='\033[0m'
say()  { printf "${c_cyan}==> %s${c_off}\n" "$*"; }
ok()   { printf "${c_grn}%s${c_off}\n" "$*"; }
err()  { printf "${c_red}%s${c_off}\n" "$*"; }
pause(){ printf "\n${c_dim}Press Enter to continue…${c_off}"; read -r _; }

cfg_get() {  # cfg_get key default
  [ -x "$PY" ] || { echo "$2"; return; }
  "$PY" - "$APP_DIR/config.json" "$1" "$2" <<'PY' 2>/dev/null || echo "$2"
import json, sys
p, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(p, encoding="utf-8") as fh: cfg = json.load(fh)
    print(cfg.get(key, default))
except Exception:
    print(default)
PY
}

show_status() {
  say "Services"
  systemctl --no-pager --full status "$SERVICE" 2>/dev/null | sed -n '1,4p'
  echo
  systemctl --no-pager --full status "$DASH" 2>/dev/null | sed -n '1,4p'
  echo
  say "Last cycle"
  if [ -f "$APP_DIR/state/status.json" ]; then
    "$PY" - "$APP_DIR/state/status.json" <<'PY' 2>/dev/null || cat "$APP_DIR/state/status.json"
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh: s = json.load(fh)
print(f"  published : {s.get('count','?')}")
print(f"  updated_at: {s.get('updated_at','?')}")
print(f"  cycle time: {s.get('duration_s','?')}s")
print(f"  ok        : {s.get('published_ok','?')}   signed: {s.get('signed','?')}")
PY
  else
    echo "  (no cycle has completed yet)"
  fi
  echo
  say "Auto-update"
  if systemctl is-enabled "$AUTO_TIMER" >/dev/null 2>&1; then
    ok "  enabled  ($(systemctl is-active "$AUTO_TIMER" 2>/dev/null))"
    systemctl --no-pager list-timers "$AUTO_TIMER" 2>/dev/null | sed -n '2p'
  else
    echo "  disabled"
  fi
  pause
}

hard_update()     { say "Hard-updating…"; sudo bash "$APP_DIR/update.sh"; pause; }
restart_services(){ say "Restarting…"; sudo systemctl restart "$SERVICE" "$DASH" && ok "Restarted."; pause; }
live_logs()       { say "Live logs — Ctrl-C to return."; sudo journalctl -u "$SERVICE" -f -n 80; }
run_once()        { say "Running one test cycle (foreground)…"; "$PY" -m zonevpn --once; pause; }
edit_config()     { "$PY" "$APP_DIR/setup_wizard.py"; say "Restarting to apply…"; sudo systemctl restart "$SERVICE"; pause; }
migrate_now()     { say "Migrating config structure…"; "$PY" -m zonevpn.migrate; pause; }

toggle_autoupdate() {
  if systemctl is-enabled "$AUTO_TIMER" >/dev/null 2>&1; then
    say "Disabling automatic updates…"
    sudo systemctl disable --now "$AUTO_TIMER" && ok "Auto-update OFF."
  else
    say "Enabling automatic updates (hourly check)…"
    sudo systemctl enable --now "$AUTO_TIMER" && ok "Auto-update ON." \
      || err "Timer not installed — run: sudo bash $APP_DIR/install.sh"
  fi
  pause
}

dashboard_info() {
  local ip port token
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  port="$(cfg_get dashboard_port 8787)"
  token="$(cfg_get dashboard_token '')"
  say "Dashboard"
  echo "  http://${ip:-SERVER_IP}:${port}/?token=${token}"
  pause
}

menu() {
  clear
  printf "${c_grn}┌──────────────────────────────────────────────┐${c_off}\n"
  printf "${c_grn}│            ZoneVPN · server control          │${c_off}\n"
  printf "${c_grn}└──────────────────────────────────────────────┘${c_off}\n"
  printf "  dir: ${c_dim}%s${c_off}\n\n" "$APP_DIR"
  echo "  1) Status & last cycle"
  echo "  2) Live logs"
  echo "  3) Hard update now (pull + migrate + restart)"
  echo "  4) Restart services"
  echo "  5) Run one test cycle now (--once)"
  echo "  6) Edit config (setup wizard)"
  echo "  7) Migrate config structure now"
  echo "  8) Toggle automatic updates"
  echo "  9) Dashboard URL & token"
  echo "  0) Quit"
  printf "\n  choose: "
}

while true; do
  menu
  read -r choice
  case "$choice" in
    1) show_status ;;
    2) live_logs ;;
    3) hard_update ;;
    4) restart_services ;;
    5) run_once ;;
    6) edit_config ;;
    7) migrate_now ;;
    8) toggle_autoupdate ;;
    9) dashboard_info ;;
    0|q|Q) exit 0 ;;
    *) ;;
  esac
done
