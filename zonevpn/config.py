"""Load config.json and locate the xray binary."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


def load() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"config.json not found at {CONFIG_PATH}\n"
            f"Run the setup wizard first:  python setup_wizard.py"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("test", {})
    return cfg


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
