"""Name surviving configs '<flag> zone-vpn-<id>' and build the output payload."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import List

from .geo import flag_emoji
from .links import ParsedConfig, canonical_link, node_key, rebuild_link

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _stable_suffix(cfg: ParsedConfig, salt: int = 0, n: int = 5) -> str:
    """A short name derived from the node itself, the same every cycle.

    It used to be random, and that cost more than it looked. The app keys the
    user's pick and its live session on an id hashed from the whole link - name
    included - so a node renamed every cycle was a new server every cycle as
    far as the app was concerned. The app refreshes every three minutes, so a
    user who chose Germany was back on Auto within three minutes of choosing
    it, and their next connect went wherever was fastest. The name gives away
    nothing more for being stable: it is a hash, not an address.
    """
    digest = hashlib.sha1(f"{salt}|{node_key(cfg.raw)}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    out = []
    for _ in range(n):
        value, i = divmod(value, len(_ALPHABET))
        out.append(_ALPHABET[i])
    return "".join(out)


def build_output(configs: List[ParsedConfig], name_prefix: str = "zone-vpn") -> dict:
    """configs must already be in publishing order."""
    used: set[str] = set()
    items = []
    raw_links = []
    for cfg in configs:
        flag = flag_emoji(cfg.country)
        salt = 0
        while True:
            name = f"{flag} {name_prefix}-{_stable_suffix(cfg, salt)}"
            if name not in used:
                used.add(name)
                break
            salt += 1  # deterministic, so a collision resolves the same way
        new_link = rebuild_link(canonical_link(cfg.raw), name)
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
