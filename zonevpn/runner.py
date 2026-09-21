"""One full cycle: collect -> test -> geo-annotate -> rename -> publish."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List

from . import config as cfgmod
from . import gist, links, sign, sources, state
from .geo import GeoResolver
from .links import ParsedConfig
from .reliability import Reliability
from .rename import build_output
from .tester import Tester

log = logging.getLogger("zonevpn.runner")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _progress(phase: str, active: bool = True, **extra) -> None:
    """Write the live cycle progress the dashboard polls. Best-effort."""
    try:
        state.write_progress({"phase": phase, "active": active,
                              "updated_at": _now_iso(), **extra})
    except Exception:
        pass


def _merge_manual(configs: List[ParsedConfig]) -> List[ParsedConfig]:
    """Append operator-added servers (from the dashboard) and flag them so the
    rest of the pipeline never drops them, even if they're slow/fail testing."""
    manual = state.read_manual()
    if not manual:
        return configs
    manual_keys = set()
    parsed_manual: List[ParsedConfig] = []
    for raw in manual:
        parsed = links.parse_link(raw)
        if parsed is None:
            continue
        parsed.manual = True
        parsed_manual.append(parsed)
        manual_keys.add(links.dedup_key(parsed))

    # Flag any already-collected config that the operator also pinned.
    for c in configs:
        if links.dedup_key(c) in manual_keys:
            c.manual = True

    seen = {links.dedup_key(c) for c in configs}
    added = 0
    for parsed in parsed_manual:
        if links.dedup_key(parsed) in seen:
            continue
        seen.add(links.dedup_key(parsed))
        configs.append(parsed)
        added += 1
    if added:
        log.info("manual servers: added %d from the dashboard", added)
    return configs


def _ensure_manual(alive: List[ParsedConfig],
                   manual_by_key: dict) -> List[ParsedConfig]:
    """Make sure every operator-pinned server is in the output, even if it
    didn't pass testing (it'll show with an unknown ping until it works)."""
    present = {links.dedup_key(c) for c in alive}
    for key, mc in manual_by_key.items():
        if key not in present:
            alive.append(mc)
    return alive


async def run_cycle(cfg: dict, xray_path: str, geo: GeoResolver) -> bool:
    t0 = time.monotonic()
    test_cfg = cfg.get("test", {})

    # allowInsecure in TLS outbounds (needs an xray build that still supports it)
    links.ALLOW_INSECURE = bool(test_cfg.get("tls_allow_insecure", False))

    # Concurrency knobs, surfaced to the dashboard as the active "threads".
    threads = {
        "parallel_batches": int(test_cfg.get("parallel_batches", 4)),
        "measure_concurrency": int(test_cfg.get("measure_concurrency", 32)),
        "batch_size": int(test_cfg.get("batch_size", 100)),
    }

    # 1. collect
    _progress("collecting", threads=threads)
    configs = await sources.collect(cfg.get("sources", []))
    configs = _merge_manual(configs)
    if not configs:
        log.warning("no configs collected; skipping cycle")
        _progress("idle", active=False)
        return False
    collected = len(configs)
    manual_by_key = {links.dedup_key(c): c for c in configs if c.manual}

    # 1b. drop anything the operator deleted from the dashboard (by address:port)
    blocked = set(state.load_blocklist())
    if blocked:
        before = len(configs)
        configs = [c for c in configs
                   if state.block_key(c.address, c.port) not in blocked]
        if before != len(configs):
            log.info("blocklist: dropped %d server(s)", before - len(configs))

    pool = configs
    configs = _select_for_testing(test_cfg, configs)

    tester = Tester(xray_path, test_cfg)

    # 2. optional TCP pre-filter (OFF by default now — it falsely drops working
    # CDN/domain-fronted configs; testing everything via xray yields more).
    if test_cfg.get("tcp_prefilter", False):
        _progress("prefilter", threads=threads, collected=collected)
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
            _progress("idle", active=False)
            return False
    reachable = len(configs)

    # 3. real-delay test through xray (live progress -> dashboard)
    def _on_test_progress(snap: dict) -> None:
        _progress("testing", threads=threads, collected=collected,
                  reachable=reachable, **snap)

    _progress("testing", threads=threads, collected=collected,
              reachable=reachable, tested=0, total=reachable, alive=0, recent=[])
    alive = await tester.run(configs, progress_cb=_on_test_progress)
    # Operator-pinned servers are always kept, even if they failed the test.
    alive = _ensure_manual(alive, manual_by_key)
    log.info("alive after testing: %d / %d (%.1fs)",
             len(alive), reachable, time.monotonic() - t0)
    if tester.filtered_out:
        # Worth its own line even when it is not fatal: these were fast and
        # healthy and could not reach anything blocked, so they are the
        # difference between what the tunnel test found and what ships. If this
        # number ever approaches the whole survivor list, suspect the check.
        log.info("censorship check rejected %d healthy config(s)",
                 len(tester.filtered_out))
    _log_source_yield(configs, alive)
    if not alive and tester.filtered_out:
        # Everything healthy failed the censorship check. Far likelier that the
        # check is broken here than that every working server simultaneously
        # stopped circumventing, so take the pre-check result for this cycle
        # and make the reason loud rather than publishing nothing.
        log.warning("censorship check rejected ALL %d healthy configs — "
                    "falling back to the plain result for this cycle; "
                    "check `censored_urls` reachability from this server",
                    len(tester.filtered_out))
        alive = _ensure_manual(list(tester.filtered_out), manual_by_key)

    if not alive:
        log.warning("no config passed the test; not publishing")
        _progress("idle", active=False)
        return False

    # 3b. fold this cycle's result into each endpoint's short history, then
    # refuse to publish the ones that keep letting users down.
    #
    # The test is a snapshot and these nodes are not stable at that timescale:
    # one that passes now and dies in four minutes still reaches a user as a
    # working server. A node has to earn its place over several cycles before
    # the score is allowed to reject it, so newly discovered ones are never
    # locked out.
    alive = _apply_reliability(cfg, configs, alive, pool)
    if not alive:
        log.warning("nothing passed the reliability bar; not publishing")
        _progress("idle", active=False)
        return False

    # 4. publish only genuinely fast servers (real delay under the threshold) —
    # but never drop a manually-added server.
    publish_max_ping = int(test_cfg.get("publish_max_ping", 800) or 0)
    if publish_max_ping > 0:
        before = len(alive)
        alive = [c for c in alive
                 if c.manual or 0 < c.ping <= publish_max_ping]
        log.info("fast filter (<=%dms): %d -> %d", publish_max_ping, before, len(alive))
        if not alive:
            log.warning("no server under %dms; not publishing", publish_max_ping)
            _progress("idle", active=False)
            return False

    # 5. geo annotate. Prefer the REAL exit country learned through the tunnel
    # (tester._annotate_exit). For the rest, geolocate the measured exit IP when
    # we have one (accurate), else the front address (a CDN address geos wrong).
    need_geo = [c for c in alive if not c.country]
    if need_geo:
        cc_map = await geo.annotate([(c.exit_ip or c.address) for c in need_geo])
        for c in need_geo:
            c.country = cc_map.get(c.exit_ip or c.address, "")

    # 5b. Drop servers whose REAL exit is Iran — a tunnel that exits inside the
    # user's own (restricted) country is useless and misleading, so it never
    # makes the list.
    before = len(alive)
    alive = [c for c in alive if (c.country or "").upper() != "IR"]
    if before != len(alive):
        log.info("dropped %d server(s) with an Iran exit", before - len(alive))
    if not alive:
        log.warning("everything exited via Iran; not publishing")
        _progress("idle", active=False)
        return False

    # 6. trim (manual servers are exempt — they always stay)
    max_out = int(test_cfg.get("max_output", 0) or 0)
    if max_out and len(alive) > max_out:
        manual_part = [c for c in alive if c.manual]
        others = [c for c in alive if not c.manual]
        keep = max(0, max_out - len(manual_part))
        others = _trim(others, keep, int(test_cfg.get("min_per_country", 0) or 0))
        alive = manual_part + others

    # Final order: proven first, then by real delay.
    #
    # Sorting on latency alone is what put the least reliable servers at the top
    # of the user's list: the fastest nodes are the nearest ones, and the
    # nearest ones are the most likely to be locally throttled. A node with a
    # track record and 200 ms beats an unknown at 90 ms.
    alive.sort(key=lambda c: (
        -_reliability_of(c),
        c.ping if c.ping and c.ping > 0 else 10 ** 9,
    ))

    # 7. build payload + publish
    _progress("publishing", threads=threads, collected=collected,
              reachable=reachable, alive=len(alive))
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

    # 8. snapshot local state for the dashboard (decoded list + stats)
    _write_state(alive, payload, ok, bool(sign_key),
                 bool(cfg.get("gist_base64", True)), time.monotonic() - t0)
    _progress("idle", active=False, published=payload.get("count", len(alive)),
              duration_s=round(time.monotonic() - t0, 1))
    return ok


def _select_for_testing(test_cfg: dict,
                        configs: List[ParsedConfig]) -> List[ParsedConfig]:
    """Which of the pool to test this cycle, when it is too big to finish.

    Cycle time is the freshness of the published list: these nodes die in
    minutes, so a list rebuilt every twenty is half wrong by the time the app
    reads it. On a 2-core box the pool outgrew the interval, and the old answer
    here - `configs[:limit]` - was the worst one available, because it tested
    the same prefix every cycle and never looked at the rest at all.

    Three groups, in order of what they are worth:

      * everything with a track record, tested every cycle without exception.
        That is the published list and its near misses, it is small (tens), and
        re-verifying it is the whole job - a server that stopped working has to
        leave the list quickly.
      * then whole sources, best first, until half the budget is spent. A
        source's worth is how many of the endpoints it offers have ever proved
        themselves, and measured over six cycles that ratio spans two orders of
        magnitude: one repo ran at 20% and another returned nothing from 12989
        tested endpoints. Splitting the budget evenly across the pool spends
        almost all of it on the second kind. The first plain rotation did
        exactly that and tested 3 of the best source's 24 configs.
      * and a rotating window over everything left, so the warehouses are still
        swept and a new source can still prove itself - just not at the cost of
        the list.

    Sources nothing has ever come from are ordered smallest first, so a new
    small repo gets a full trial within a cycle or two instead of waiting for
    the rotation to reach it.
    """
    limit = int(test_cfg.get("max_configs_to_test", 0) or 0)
    if not limit or len(configs) <= limit:
        return configs

    rel = Reliability.load(int(test_cfg.get("reliability_window", 6) or 6))

    def proved(c: ParsedConfig) -> bool:
        key = state.block_key(c.address, c.port)
        return rel.samples(key) > 0 and rel.score(key) > 0

    proven: List[ParsedConfig] = []
    by_src: dict = {}
    for c in configs:
        if c.manual or proved(c):
            proven.append(c)
        else:
            by_src.setdefault(c.extra.get("src", "?"), []).append(c)

    budget = limit - len(proven)
    if not by_src or budget <= 0:
        log.info("testing %d known-good config(s) only; the pool is %d",
                 len(proven), len(configs))
        return proven

    # What each source has been worth: proven endpoints per endpoint offered.
    earned: dict = {}
    for c in proven:
        src = c.extra.get("src", "?")
        earned[src] = earned.get(src, 0) + 1
    def worth(src: str) -> tuple:
        offered = len(by_src[src]) + earned.get(src, 0)
        rate = earned.get(src, 0) / offered
        # Unproven sources tie at 0; prefer the small ones, they are cheap to
        # settle either way.
        return (-rate, len(by_src[src]))

    ranked = sorted(by_src, key=worth)

    whole: List[ParsedConfig] = []
    guaranteed = budget // 2
    taken = []
    for src in ranked:
        group = by_src[src]
        if len(whole) + len(group) > guaranteed:
            # Skip rather than stop: a big source should not block the smaller
            # ones behind it out of the guarantee. It still gets swept below.
            continue
        whole.extend(group)
        taken.append(src)
    for src in taken:
        del by_src[src]

    rest = [c for src in ranked if src in by_src for c in by_src[src]]
    take = max(0, budget - len(whole))
    window: List[ParsedConfig] = []
    if rest and take:
        cursor = state.read_cursor() % len(rest)
        window = rest[cursor:cursor + take]
        if len(window) < take:                     # wrap around the end
            window += rest[:take - len(window)]
        state.write_cursor((cursor + len(window)) % len(rest))

    log.info("testing %d of %d: %d proven + %d from %d source(s) that produce "
             "+ %d rotating of %d",
             len(proven) + len(whole) + len(window), len(configs),
             len(proven), len(whole), len(taken),
             len(window), len(rest))
    return proven + whole + window


def _log_source_yield(tested: List[ParsedConfig],
                      alive: List[ParsedConfig]) -> None:
    """Report what each source was worth this cycle.

    Raw link counts say nothing: these repositories copy from each other, and
    the biggest of them can contribute nothing the pool does not already have.
    What is worth knowing is how many *working* servers a source is the origin
    of, which is the only basis on which to add or drop one. Credit goes to the
    source that listed an endpoint first (see [sources.collect]).
    """
    if not tested:
        return
    total: dict = {}
    won: dict = {}
    for c in tested:
        src = c.extra.get("src", "?")
        total[src] = total.get(src, 0) + 1
    for c in alive:
        src = c.extra.get("src", "?")
        won[src] = won.get(src, 0) + 1
    rows = sorted(total.items(), key=lambda kv: (-won.get(kv[0], 0), -kv[1]))
    for src, n in rows:
        log.info("source yield: %5d alive / %5d tested  %s",
                 won.get(src, 0), n, src)


def _reliability_of(c: ParsedConfig) -> float:
    """The score stashed on the config in [_apply_reliability]."""
    try:
        return float(c.extra.get("reliability", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _apply_reliability(cfg: dict, tested: List[ParsedConfig],
                       alive: List[ParsedConfig],
                       pool: List[ParsedConfig]) -> List[ParsedConfig]:
    """Record this cycle's outcome per endpoint and drop the chronic failures.

    Every config that went into the test is recorded, not just the survivors —
    a failure is the more informative half of the history.
    """
    test_cfg = cfg.get("test", {})
    window = int(test_cfg.get("reliability_window", 6) or 6)
    min_score = float(test_cfg.get("min_reliability", 0.5) or 0)
    min_samples = int(test_cfg.get("reliability_min_samples", 3) or 3)

    rel = Reliability.load(window)
    passed = {state.block_key(c.address, c.port) for c in alive}
    for c in tested:
        rel.record(state.block_key(c.address, c.port),
                   state.block_key(c.address, c.port) in passed)

    kept: List[ParsedConfig] = []
    dropped = 0
    for c in alive:
        key = state.block_key(c.address, c.port)
        score = rel.score(key)
        c.extra["reliability"] = round(score, 3)
        c.extra["reliability_samples"] = rel.samples(key)
        if c.manual or min_score <= 0 or rel.is_trusted(key, min_score, min_samples):
            kept.append(c)
        else:
            dropped += 1
    if dropped:
        log.info("reliability filter (>=%.0f%% of last %d cycles): dropped %d",
                 min_score * 100, window, dropped)

    # Prune against the WHOLE pool, not this cycle's slice. With rotation on,
    # most endpoints are not tested in any given cycle, and pruning to the
    # slice would throw away the history of everything that was not - which is
    # exactly the history the rotation depends on to know what is proven.
    rel.prune(state.block_key(c.address, c.port) for c in pool)
    try:
        rel.save()
    except Exception:
        log.exception("failed to save reliability history (non-fatal)")
    return kept


def _write_state(final: List[ParsedConfig], payload: dict, ok: bool,
                 signed: bool, base64_encoded: bool, duration_s: float) -> None:
    """Persist a decoded snapshot so the dashboard can show readable rows and
    offer per-server delete (the gist itself is base64/obfuscated)."""
    try:
        servers = []
        for parsed, item in zip(final, payload.get("configs", [])):
            servers.append({**item,
                            "exit_ip": parsed.exit_ip,
                            "tcp_ping": parsed.tcp_ping,
                            "reliability": parsed.extra.get("reliability"),
                            "kbps": parsed.extra.get("kbps"),
                            "front": f"{parsed.address}:{parsed.port}",
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
