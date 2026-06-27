"""Rename surviving configs to '<flag> zone-vpn-<random>' and build the output payload."""

from __future__ import annotations

import random
import string
from datetime import datetime, timezone
from typing import List

from .geo import flag_emoji
from .links import ParsedConfig, rebuild_link

_ALPHABET = string.ascii_lowercase + string.digits


def _random_suffix(n: int = 5) -> str:
    return "".join(random.choices(_ALPHABET, k=n))


def build_output(configs: List[ParsedConfig], name_prefix: str = "zone-vpn") -> dict:
    """configs must already be sorted by ping ascending."""
    used: set[str] = set()
    items = []
    raw_links = []
    for cfg in configs:
        flag = flag_emoji(cfg.country)
        while True:
            suffix = _random_suffix()
            name = f"{flag} {name_prefix}-{suffix}"
            if name not in used:
                used.add(name)
                break
        new_link = rebuild_link(cfg.raw, name)
        raw_links.append(new_link)
        items.append({
            "name": name,
            "ping": cfg.ping,
            "country": cfg.country or "??",
            "flag": flag,
            "protocol": cfg.protocol,
            "config": new_link,
        })

    return {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "configs": items,
        # convenience: newline-joined links, ready to be used as a raw subscription
        "raw": "\n".join(raw_links),
    }
