#!/usr/bin/env python3
"""Interactive setup wizard for ZoneVPN.

Asks for the sensitive / deployment-specific values (GitHub token, gist, sources,
interval) and writes config.json with strict file permissions. Safe to re-run:
existing values are shown as defaults.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"


def _load_existing() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def ask_int(prompt: str, default: int) -> int:
    while True:
        val = input(f"{prompt} [{default}]: ").strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            print("  -> please enter a number")


def main() -> None:
    print("=" * 60)
    print(" ZoneVPN  -  setup wizard")
    print("=" * 60)

    cfg = _load_existing()
    cfg.pop("_comment", None)

    # ---- GitHub token --------------------------------------------------- #
    print("\n1) GitHub token (needs the 'gist' scope).")
    print("   Create one at: https://github.com/settings/tokens?type=beta")
    token = ask("   GitHub token", cfg.get("github_token", ""))
    if not token:
        print("   ! token is required to publish the list. Aborting.")
        sys.exit(1)
    cfg["github_token"] = token

    # ---- Gist ----------------------------------------------------------- #
    print("\n2) Gist to publish into.")
    filename = ask("   Gist filename", cfg.get("gist_filename", "zone-vpn.json"))
    cfg["gist_filename"] = filename
    gist_id = ask("   Existing gist id (leave empty to create a new one)",
                  cfg.get("gist_id", ""))

    if not gist_id:
        print("   Creating a new public gist ...")
        try:
            from zonevpn import gist as gistmod
            gist_id = gistmod.create_gist(
                token, filename,
                json.dumps({"updated_at": None, "count": 0, "configs": []}, indent=2),
                "ZoneVPN free V2Ray configs (auto-updated)",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"   ! could not create gist automatically: {exc}")
            gist_id = ""
        if gist_id:
            print(f"   -> created gist: {gist_id}")
        else:
            print("   ! please create a gist manually and paste its id.")
            gist_id = ask("   Gist id", "")
    cfg["gist_id"] = gist_id

    if gist_id:
        raw = f"https://gist.githubusercontent.com/raw/{gist_id}/{filename}"
        cfg["_raw_url_hint"] = raw

    # ---- sources -------------------------------------------------------- #
    print("\n3) Config source repositories (raw URLs).")
    print("   Current sources:")
    for s in cfg.get("sources", []):
        print(f"     - {s}")
    print("   Paste new raw URLs one per line. Empty line = keep current list.")
    new_sources = []
    while True:
        line = input("   url> ").strip()
        if not line:
            break
        new_sources.append(line)
    if new_sources:
        cfg["sources"] = new_sources

    # ---- misc ----------------------------------------------------------- #
    print("\n4) Tuning")
    cfg["name_prefix"] = ask("   Name prefix", cfg.get("name_prefix", "zone-vpn"))
    cfg["interval_minutes"] = ask_int("   Update interval (minutes)",
                                      int(cfg.get("interval_minutes", 10)))
    test = cfg.setdefault("test", {})
    test["max_output"] = ask_int("   Max configs to publish",
                                 int(test.get("max_output", 300)))
    test["parallel_batches"] = ask_int("   Parallel test batches (lower = lighter server)",
                                       int(test.get("parallel_batches", 4)))

    # ---- write ---------------------------------------------------------- #
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except OSError:
        pass

    print("\n" + "=" * 60)
    print(f" Saved -> {CONFIG_PATH}")
    if gist_id:
        print(f" Your list will be at:\n   https://gist.githubusercontent.com/raw/{gist_id}/{filename}")
    print(" Test one cycle now with:   python -m zonevpn --once")
    print("=" * 60)


if __name__ == "__main__":
    main()
