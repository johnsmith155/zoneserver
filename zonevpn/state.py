"""Local state shared between the collector (writes) and the dashboard (reads).

Everything lives under `<repo>/state/` and is *git-ignored* so a hard update
(`git reset --hard`) never touches it:

  state/status.json     last-cycle stats (counts, timing, ok flag)
  state/servers.json    the *decoded* published list + a stable `block_key`
                        per item, so the dashboard can show readable rows and
                        offer a delete button even though the gist is base64.
  state/blocklist.json  list of block_keys the operator deleted; the collector
                        filters these out every cycle.
  state/zonevpn.log     rotating log file the dashboard tails for "live logs".

`block_key` is `"<address>:<port>"` — stable across cycles even though each
server is renamed/re-encoded every run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("zonevpn.state")

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"

STATUS_FILE = STATE_DIR / "status.json"
SERVERS_FILE = STATE_DIR / "servers.json"
BLOCKLIST_FILE = STATE_DIR / "blocklist.json"
PROGRESS_FILE = STATE_DIR / "progress.json"
MANUAL_FILE = STATE_DIR / "manual.json"
LOG_FILE = STATE_DIR / "zonevpn.log"


def ensure_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def block_key(address: str, port: int) -> str:
    return f"{address}:{port}"


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so a reader never sees a half file."""
    ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# blocklist                                                                     #
# --------------------------------------------------------------------------- #
def load_blocklist() -> List[str]:
    data = _read_json(BLOCKLIST_FILE, [])
    return list(data) if isinstance(data, list) else []


def save_blocklist(keys: List[str]) -> None:
    # de-dupe, keep order
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    _atomic_write(BLOCKLIST_FILE, json.dumps(out, indent=2))


def add_to_blocklist(key: str) -> List[str]:
    keys = load_blocklist()
    if key not in keys:
        keys.append(key)
        save_blocklist(keys)
    return keys


def remove_from_blocklist(key: str) -> List[str]:
    keys = [k for k in load_blocklist() if k != key]
    save_blocklist(keys)
    return keys


# --------------------------------------------------------------------------- #
# status + servers snapshots (written by the collector each cycle)             #
# --------------------------------------------------------------------------- #
def write_status(status: dict) -> None:
    _atomic_write(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=2))


def read_status() -> dict:
    return _read_json(STATUS_FILE, {})


def write_servers(servers: List[dict]) -> None:
    _atomic_write(SERVERS_FILE, json.dumps(servers, ensure_ascii=False, indent=2))


def read_servers() -> List[dict]:
    data = _read_json(SERVERS_FILE, [])
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------- #
# live cycle progress (written continuously while a cycle runs)                #
# --------------------------------------------------------------------------- #
def write_progress(progress: dict) -> None:
    _atomic_write(PROGRESS_FILE, json.dumps(progress, ensure_ascii=False))


def read_progress() -> dict:
    data = _read_json(PROGRESS_FILE, {})
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# manual servers (added from the dashboard, tested every cycle like the rest)  #
# --------------------------------------------------------------------------- #
def read_manual() -> List[str]:
    data = _read_json(MANUAL_FILE, [])
    return [s for s in data if isinstance(s, str)] if isinstance(data, list) else []


def add_manual(link: str) -> List[str]:
    link = (link or "").strip()
    links = read_manual()
    if link and link not in links:
        links.append(link)
        _atomic_write(MANUAL_FILE, json.dumps(links, ensure_ascii=False, indent=2))
    return links


def remove_manual(link: str) -> List[str]:
    links = [s for s in read_manual() if s != link]
    _atomic_write(MANUAL_FILE, json.dumps(links, ensure_ascii=False, indent=2))
    return links
