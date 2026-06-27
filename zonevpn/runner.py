"""One full cycle: collect -> test -> geo-annotate -> rename -> publish."""

from __future__ import annotations

import logging
import time
from typing import List

from . import config as cfgmod
from . import gist, sources
from .geo import GeoResolver
from .links import ParsedConfig
from .rename import build_output
from .tester import Tester

log = logging.getLogger("zonevpn.runner")


async def run_cycle(cfg: dict, xray_path: str, geo: GeoResolver) -> bool:
    t0 = time.monotonic()
    test_cfg = cfg.get("test", {})

    # 1. collect
    configs = await sources.collect(cfg.get("sources", []))
    if not configs:
        log.warning("no configs collected; skipping cycle")
        return False

    limit = int(test_cfg.get("max_configs_to_test", 0) or 0)
    if limit and len(configs) > limit:
        configs = configs[:limit]
        log.info("limited to %d configs for testing", limit)

    tester = Tester(xray_path, test_cfg)

    # 2. cheap TCP pre-filter (huge win at this scale, especially from Iran)
    if test_cfg.get("tcp_prefilter", True):
        before = len(configs)
        configs = await tester.tcp_prefilter(
            configs,
            timeout=float(test_cfg.get("tcp_timeout", 3)),
            concurrency=int(test_cfg.get("tcp_concurrency", 256)),
        )
        log.info("tcp prefilter: %d -> %d reachable (%.1fs)",
                 before, len(configs), time.monotonic() - t0)
        if not configs:
            log.warning("nothing reachable over TCP; skipping cycle")
            return False

    # 3. real-delay test through xray
    alive = await tester.run(configs)
    log.info("alive after testing: %d / %d (%.1fs)",
             len(alive), len(configs), time.monotonic() - t0)
    if not alive:
        log.warning("no config passed the test; not publishing")
        return False

    # 4. geo annotate (only the survivors -> cheap)
    cc_map = await geo.annotate([c.address for c in alive])
    for c in alive:
        c.country = cc_map.get(c.address, "")

    # 5. trim
    max_out = int(test_cfg.get("max_output", 0) or 0)
    if max_out and len(alive) > max_out:
        alive = _trim(alive, max_out, int(test_cfg.get("min_per_country", 0) or 0))

    # 6. build payload + publish
    payload = build_output(alive, cfg.get("name_prefix", "zone-vpn"))
    ok = gist.publish(cfg["github_token"], cfg["gist_id"], cfg["gist_filename"], payload)
    if ok:
        log.info("published %d configs to gist %s (%.1fs total)",
                 payload["count"], cfg["gist_id"], time.monotonic() - t0)
    else:
        log.error("gist publish failed")
    return ok


def _trim(alive: List[ParsedConfig], max_out: int, min_per_country: int) -> List[ParsedConfig]:
    """Keep the fastest configs, optionally guaranteeing a few per country for variety."""
    alive.sort(key=lambda c: c.ping)
    if min_per_country <= 0:
        return alive[:max_out]

    per: dict[str, int] = {}
    primary, overflow = [], []
    for c in alive:
        cc = c.country or "??"
        if per.get(cc, 0) < min_per_country:
            per[cc] = per.get(cc, 0) + 1
            primary.append(c)
        else:
            overflow.append(c)
    result = (primary + overflow)[:max_out]
    result.sort(key=lambda c: c.ping)
    return result
