"""Self-healing config migration.

Whenever the code adds a new setting (or a new section), the shape of
`config.example.json` changes but the operator's live `config.json` does not.
This module reconciles the two automatically so a hard update never leaves the
server running on a stale/incomplete config:

  • new keys present in the example are added with their default value,
  • new keys inside nested sections (e.g. `test`) are added too,
  • doc/comment keys (anything starting with "_") are refreshed to the latest
    wording from the example,
  • every value the operator already set — tokens, gist id, signing keys,
    tuned numbers — is preserved untouched.

It runs on every collector start (see `__main__`), from `update.sh`, and from
the `zonevpn` menu, and is a cheap no-op when nothing changed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Tuple

log = logging.getLogger("zonevpn.migrate")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"


def _merge(example: dict, current: dict) -> Tuple[dict, bool]:
    """Merge `example` defaults into `current`. Returns (merged, changed)."""
    changed = False
    out = dict(current)
    for key, ex_val in example.items():
        if key.startswith("_"):
            # Documentation/comment key: always carry the newest text across.
            if out.get(key) != ex_val:
                out[key] = ex_val
                changed = True
        elif key not in out:
            # Brand new setting: adopt the example's default.
            out[key] = ex_val
            changed = True
        elif isinstance(ex_val, dict) and isinstance(out.get(key), dict):
            merged_child, child_changed = _merge(ex_val, out[key])
            if child_changed:
                out[key] = merged_child
                changed = True
        # else: operator already has a real value for this key -> keep it.
    return out, changed


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via a 0600 temp file + rename so secrets never leak and a
    reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate_config(config_path: Path = CONFIG_PATH,
                   example_path: Path = EXAMPLE_PATH) -> bool:
    """Bring config.json up to the latest example structure in place.

    Returns True if the file was changed. Safe to call repeatedly.
    """
    if not config_path.exists() or not example_path.exists():
        return False
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            current = json.load(fh)
        with open(example_path, "r", encoding="utf-8") as fh:
            example = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("config migration skipped: %s", exc)
        return False

    if not isinstance(current, dict) or not isinstance(example, dict):
        return False

    merged, changed = _merge(example, current)
    if changed:
        _atomic_write_json(config_path, merged)
        log.info("config.json migrated to the latest structure")
    return changed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    changed = migrate_config()
    print("config.json updated to the latest structure."
          if changed else "config.json already up to date.")


if __name__ == "__main__":
    main()
