"""One full cycle: collect -> test -> geo-annotate -> rename -> publish."""

from __future__ import annotations

import logging
import time
from typing import List

from . import config as cfgmod
from . import gist, links, sign, sources, state
from .geo import GeoResolver
from .links import ParsedConfig
from .rename import build_output
from .tester import Tester

log = logging.getLogger("zonevpn.runner")


async def run_cycle(cfg: dict, xray_path: str, geo: GeoResolver) -> bool:
    t0 = time.monotonic()
    test_cfg = cfg.get("test", {})

    # allowInsecure in TLS outbounds (needs an xray build that still supports it)
    links.ALLOW_INSECURE = bool(test_cfg.get("tls_allow_insecure", False))

    # 1. collect
    configs = await sources.collect(cfg.get("sources", []))
    if not configs:
        log.warning("no configs collected; skipping cycle")
        return False

    # 1b. drop anything the operator deleted from the dashboard (by address:port)
    blocked = set(state.load_blocklist())
    if blocked:
        before = len(configs)
        configs = [c for c in configs
                   if state.block_key(c.address, c.port) not in blocked]
        if before != len(configs):
            log.info("blocklist: dropped %d server(s)", before - len(configs))

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
    sign_key = sign.load_private_key(cfg)  # None unless configured -> opt-in
    ok = gist.publish(
        cfg["github_token"], cfg["gist_id"], cfg["gist_filename"], payload,
        base64_encode=bool(cfg.get("gist_base64", True)),
        sign_key_b64=sign_key,
    )
    if sign_key:
        log.info("payload signed (Ed25519) before publish")
    if ok:
        log.info("published %d configs to gist %s (%.1fs total)",
                 payload["count"], cfg["gist_id"], time.monotonic() - t0)
    else:
        log.error("gist publish failed")

    # 7. snapshot local state for the dashboard (decoded list + stats)
    _write_state(alive, payload, ok, bool(sign_key),
                 bool(cfg.get("gist_base64", True)), time.monotonic() - t0)
    return ok


def _write_state(final: List[ParsedConfig], payload: dict, ok: bool,
                 signed: bool, base64_encoded: bool, duration_s: float) -> None:
    """Persist a decoded snapshot so the dashboard can show readable rows and
    offer per-server delete (the gist itself is base64/obfuscated)."""
    try:
        servers = []
        for parsed, item in zip(final, payload.get("configs", [])):
            servers.append({**item,
                            "block_key": state.block_key(parsed.address, parsed.port)})
        state.write_servers(servers)
        state.write_status({
            "updated_at": payload.get("updated_at"),
            "count": payload.get("count", len(servers)),
            "published_ok": ok,
            "signed": signed,
            "base64": base64_encoded,
            "duration_s": round(duration_s, 1),
        })
    except Exception:
        log.exception("failed to write dashboard state (non-fatal)")


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
