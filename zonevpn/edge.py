"""The Cloudflare edge worker (edge/src/index.js): list mirror and field reports.

## Why the collector needs to hear from phones

Every test here runs from one datacenter in Iran. Users are on mobile
networks that filter differently, and on 2026-09-22 the difference was
measured rather than assumed: a whole fleet of Shadowsocks servers passed
every test on this box and could not resolve a single name on a phone. The
app now reports what each connect attempt and each background check saw; this
module reads those reports back, and the runner lets them reorder — and, past
a clear threshold, remove — what it publishes.

Both keys live in files next to config.json (`.edge-publish-key`,
`.edge-read-key`, written by edge/deploy.sh via scp). Everything here is
best effort: an unreachable edge never stops a cycle.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("zonevpn.edge")

ROOT = Path(__file__).resolve().parent.parent


# Cloudflare answers Python's default User-Agent ("Python-urllib/3.x") with a
# 403 before the worker ever runs — bot protection, not our code. Measured from
# this box: same URL, 403 with that agent, 200 with this one.
_UA = "zoneserver/1.0"


def _key(cfg: dict, name: str) -> Optional[str]:
    path = cfg.get(f"edge_{name}_key_file") or f".edge-{name}-key"
    p = Path(path) if os.path.isabs(path) else ROOT / path
    try:
        value = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _base(cfg: dict) -> Optional[str]:
    url = (cfg.get("edge_url") or "").strip().rstrip("/")
    return url or None


def list_url(cfg: dict) -> Optional[str]:
    """Where the app can read the signed list from this mirror."""
    base = _base(cfg)
    return f"{base}/v1/l" if base else None


def report_url(cfg: dict) -> Optional[str]:
    """Where the app sends its field reports."""
    base = _base(cfg)
    return f"{base}/v1/r" if base else None


def publish_list(cfg: dict, content: str) -> bool:
    """Write the exact bytes the gist got to the mirror."""
    base, key = _base(cfg), _key(cfg, "publish")
    if not base or not key:
        return False
    req = urllib.request.Request(
        f"{base}/v1/l", data=content.encode("utf-8"), method="PUT",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "text/plain; charset=utf-8",
                 "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning("edge mirror publish failed: %s", exc)
        return False


def field_stats(cfg: dict, hours: int = 6, cc: str = "IR") -> Dict[str, dict]:
    """Per-node totals of what phones saw in the last [hours], all networks.

    {suffix: {"ok", "hs", "vf", "pok", "pfail", "ms_sum", "ms_n", "cell_ok",
    "cell_n"}}. Empty on any failure.
    """
    base, key = _base(cfg), _key(cfg, "read")
    if not base or not key:
        return {}
    req = urllib.request.Request(
        f"{base}/v1/s?hours={int(hours)}&cc={cc}",
        headers={"Authorization": f"Bearer {key}", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("edge field stats unavailable: %s", exc)
        return {}
    out: Dict[str, dict] = {}
    for row in data.get("nodes") or []:
        node = row.get("node")
        if not isinstance(node, str):
            continue
        agg = out.setdefault(node, {k: 0 for k in (
            "ok", "hs", "vf", "pok", "pfail", "ms_sum", "ms_n", "cell_ok", "cell_n")})
        for k in ("ok", "hs", "vf", "pok", "pfail", "ms_sum", "ms_n"):
            agg[k] += int(row.get(k) or 0)
        if row.get("net") == "cell":
            agg["cell_ok"] += int(row.get("ok") or 0)
            agg["cell_n"] += int(row.get("ok") or 0) + int(row.get("hs") or 0) + int(row.get("vf") or 0)
    return out


def field_rate(stats: dict) -> Optional[float]:
    """How often this node worked for real users, smoothed; None without data.

    Real connects count fully and background checks half: a check through a
    throwaway core is good evidence that a node carries, but it is not the
    tunnel the user actually gets. The +1/+2 keeps one unlucky report from
    reading as 0% or one lucky one as 100%.
    """
    attempts = stats["ok"] + stats["hs"] + stats["vf"]
    proofs = stats["pok"] + stats["pfail"]
    if attempts + proofs < 3:
        return None
    good = stats["ok"] + 0.5 * stats["pok"]
    total = attempts + 0.5 * proofs
    return (good + 1.0) / (total + 2.0)
