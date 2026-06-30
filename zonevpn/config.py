"""Load config.json and locate the xray binary."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


class ConfigError(Exception):
    """config.json is missing or not valid JSON."""


def _load_strict() -> dict:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"config.json not found at {CONFIG_PATH}")
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"config.json is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(cfg, dict):
        raise ConfigError("config.json must be a JSON object")
    cfg.setdefault("test", {})
    return cfg


def load() -> dict:
    """Strict load for the collector. Exits with a clear, actionable message
    (no stack trace) when the config is missing or malformed."""
    try:
        return _load_strict()
    except ConfigError as exc:
        raise SystemExit(
            f"{exc}\n"
            "Locate the problem with:  python3 -m json.tool config.json\n"
            "or run the `zonevpn` menu -> 'Check / fix config'. "
            "Then restart:  systemctl restart zonevpn"
        )


def load_lenient() -> Tuple[dict, Optional[str]]:
    """Never raises. Returns (cfg, error_message).

    On a broken config it still returns a usable dict for the *dashboard* by
    best-effort extracting just the dashboard_* keys from the raw text, so the
    console comes up and can show the error + logs instead of going dark too.
    """
    try:
        return _load_strict(), None
    except ConfigError as exc:
        return _extract_dashboard_keys(), str(exc)


def _extract_dashboard_keys() -> dict:
    out = {"dashboard_host": "0.0.0.0", "dashboard_port": 8787,
           "dashboard_token": "", "test": {}}
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return out
    m = re.search(r'"dashboard_host"\s*:\s*"([^"]*)"', raw)
    if m:
        out["dashboard_host"] = m.group(1)
    m = re.search(r'"dashboard_port"\s*:\s*(\d+)', raw)
    if m:
        out["dashboard_port"] = int(m.group(1))
    m = re.search(r'"dashboard_token"\s*:\s*"([^"]*)"', raw)
    if m:
        out["dashboard_token"] = m.group(1)
    return out


def find_xray(configured: str = "auto") -> str:
    """Return a usable xray executable path."""
    candidates = []
    if configured and configured != "auto":
        candidates.append(configured)
    candidates += [
        str(ROOT / "bin" / "xray"),
        str(ROOT / "bin" / "xray.exe"),
        shutil.which("xray") or "",
        "/usr/local/bin/xray",
        "/usr/bin/xray",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    # last resort: trust PATH lookup at runtime
    if shutil.which("xray"):
        return "xray"
    raise SystemExit(
        "xray binary not found. Install it (install.sh does this automatically) "
        "or set 'xray_path' in config.json."
    )


def resolve_geoip_db(cfg: dict) -> str | None:
    db = cfg.get("geoip_db")
    if not db:
        return None
    p = Path(db)
    if not p.is_absolute():
        p = ROOT / db
    return str(p) if p.exists() else None
