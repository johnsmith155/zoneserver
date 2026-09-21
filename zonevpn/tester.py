"""High-performance latency tester.

Configs are grouped into batches. Each batch is ONE xray process that exposes one
local SOCKS inbound per config and routes each inbound to its matching outbound.
We then measure the real HTTP delay of every config in the batch concurrently.
Several batches run in parallel. This keeps the number of spawned processes tiny
even when testing thousands of configs, which is what keeps the Iran server light.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from typing import Callable, List, Optional

import aiohttp
from aiohttp_socks import ProxyConnector

from .links import ParsedConfig

log = logging.getLogger("zonevpn.tester")

# xray names the outbound it could not build:
#   "failed to build outbound config with tag out12 > ..."
_BAD_TAG = re.compile(r"tag out(\d+)")


class Tester:
    def __init__(self, xray_path: str, cfg: dict):
        self.xray_path = xray_path
        self.test_url: str = cfg.get("test_url", "https://www.google.com/generate_204")
        self.expected = set(cfg.get("expected_status", [204, 200]))
        self.timeout: float = float(cfg.get("timeout", 6))
        self.batch_size: int = int(cfg.get("batch_size", 100))
        self.parallel_batches: int = int(cfg.get("parallel_batches", 4))
        self.base_port: int = int(cfg.get("base_port", 20000))
        self.max_ping: int = int(cfg.get("max_ping", 3000))

        # Learn each config's REAL exit by asking Cloudflare's trace endpoint
        # *through the tunnel*. Many free configs are CDN-fronted (Cloudflare,
        # etc.) or chained, so the share-link address is NOT where traffic
        # actually egresses — geolocating that address gives the wrong flag
        # (a server can look like it's in Iran when it isn't). The trace returns
        # `ip=` (the true egress IP) and `loc=` (its country), which is correct.
        self.geo_via_tunnel: bool = bool(cfg.get("geo_via_tunnel", True))
        self.trace_url: str = cfg.get(
            "trace_url", "https://www.cloudflare.com/cdn-cgi/trace")
        # Fallback egress-IP echo used when the trace is unreachable, so a
        # config's country can still be resolved (the runner geolocates the IP).
        self.ip_echo_url: str = cfg.get("ip_echo_url", "https://api.ipify.org")

        # ACCURACY-CRITICAL: how many delay measurements may run *at the same
        # instant* across ALL batches. The batches spin up thousands of proxies
        # cheaply, but if we fire all of their HTTP probes at once the CPU/NIC
        # saturate and every ping is wrong (a 100ms server looks like 2000ms).
        # Capping in-flight probes keeps each measurement uncontended -> real
        # pings -> the *actually fastest* servers get published. Lower = more
        # accurate but slower; this is the knob behind "test 6 at a time".
        self.measure_concurrency: int = int(cfg.get("measure_concurrency", 32))
        # Probe each surviving config a few times over a kept-alive connection
        # and keep the MIN (the warm round-trip), so a one-off TLS-handshake
        # spike doesn't misrank a good server. Dead configs fail on probe #1 and
        # never pay for extra samples.
        self.ping_samples: int = max(1, int(cfg.get("ping_samples", 2)))

        # --- The screening pass ---------------------------------------------
        #
        # Wide on purpose. Nearly every config in the pool is dead or blocked
        # from here, and finding that out costs a socket sitting on a timeout,
        # not a core - so running many at once buys throughput almost for free,
        # and the pings it sees are garbage we throw away anyway.
        self.screen_concurrency: int = int(cfg.get("screen_concurrency", 96))
        # And patient on purpose: a cold TLS handshake to a distant node from
        # Iran is routinely seconds, and anything dropped here is never
        # measured at all. Defaults to `timeout`, the looser of the two.
        self.screen_timeout: float = float(
            cfg.get("screen_timeout", 0) or 0) or self.timeout

        # A second chance for the endpoints that matter. Measured on this
        # pool: re-screening 54 endpoints known to be working recovers only
        # about 78% of them on any single pass - not contention (32 at a time
        # scored no better than 256), just how unsteady these nodes are minute
        # to minute. One retry takes that to roughly 95%, and it costs one
        # probe per failure rather than a second pass over the pool, because
        # only the endpoints the runner marked as worth it are retried.
        self.screen_retries: int = int(cfg.get("screen_retries", 1))

        # How the unparseable configs are found. See [sift].
        self.sift_chunk: int = int(cfg.get("sift_chunk", 200))
        self.sift_concurrency: int = int(cfg.get("sift_concurrency", 4))

        # --- Does the tunnel actually get past the filter? ------------------
        #
        # `test_url` defaults to a Google 204, and Google is not blocked here.
        # That makes it a fine latency probe and a poor *censorship* probe: a
        # config whose outbound quietly degrades to something direct, or whose
        # proxy is up but only reaches the open internet, passes it and gets
        # published. Measured from the app's side, a large share of published
        # nodes accepted a TCP connect in ~50 ms and then carried nothing the
        # user cared about.
        #
        # So every survivor is asked for something this network *blocks*. Only
        # the tunnel can produce that answer, which is the whole point. Several
        # targets, first one to answer wins: any single site can be slow, rate
        # limited or having a bad day, and one false negative costs a working
        # server.
        self.censored_urls: List[str] = list(cfg.get("censored_urls", [
            "https://www.youtube.com/generate_204",
            "https://t.me/s/telegram",
            "https://x.com/robots.txt",
            "https://www.instagram.com/favicon.ico",
        ]))
        self.require_censored: bool = bool(cfg.get("require_censored", True))
        self.censored_timeout: float = float(cfg.get("censored_timeout", 6))

        # --- Does it carry at a usable rate? --------------------------------
        #
        # A node that handshakes and then trickles is technically alive and
        # useless. Off by default because it costs real bandwidth on the VPS:
        # `throughput_bytes` per surviving config, every cycle.
        self.min_kbps: float = float(cfg.get("min_kbps", 0) or 0)
        self.throughput_url: str = cfg.get(
            "throughput_url", "https://speed.cloudflare.com/__down?bytes=65536")
        self.throughput_bytes: int = int(cfg.get("throughput_bytes", 65536))
        self.throughput_timeout: float = float(cfg.get("throughput_timeout", 10))

        # Bound the wait per probe so a hanging server can't stall the cycle:
        # nothing slower than max_ping can win anyway, so don't wait much past it.
        self.probe_timeout: float = min(
            self.timeout, self.max_ping / 1000.0 + 1.0
        )

        # Created in run(), bound to the running event loop.
        self._measure_sem: Optional[asyncio.Semaphore] = None
        # Free port ranges; holding one is what entitles a batch to an xray
        # process, so its size is the real process limit. See [_run_batch].
        self._ports: Optional[asyncio.Queue] = None
        # Which pass is running: "screen" or "measure".
        self._mode: str = "measure"

        # Live progress (for the dashboard). Reset at the start of every run().
        self._progress_cb: Optional[Callable[[dict], None]] = None
        self._total = 0
        self._tested = 0
        self._alive = 0
        self._recent: List[dict] = []
        self._last_emit = 0.0

        # Configs that were fast and healthy but could not reach a blocked
        # destination. Kept so the runner has something to fall back on if the
        # censorship check ever rejects *everything* — which would mean the
        # check itself is broken (all four targets down, or this VPS newly
        # unable to reach them), not that every server died at once. Publishing
        # nothing because of our own probe would be a self-inflicted outage.
        self.filtered_out: List[ParsedConfig] = []

    async def tcp_prefilter(self, configs: List[ParsedConfig],
                            timeout: float, concurrency: int) -> List[ParsedConfig]:
        """Cheaply drop servers that don't even accept a TCP connection.

        From inside Iran this also removes IPs that are network-level blocked,
        which is the bulk of the dead weight. No xray process is spawned here.
        """
        sem = asyncio.Semaphore(concurrency)

        async def check(cfg: ParsedConfig):
            async with sem:
                writer = None
                start = time.monotonic()
                try:
                    fut = asyncio.open_connection(cfg.address, cfg.port)
                    _, writer = await asyncio.wait_for(fut, timeout=timeout)
                    # Record the raw TCP handshake time (tcping) for every config.
                    cfg.tcp_ping = int((time.monotonic() - start) * 1000)
                    return cfg
                except Exception:
                    return None
                finally:
                    if writer is not None:
                        try:
                            writer.close()
                        except Exception:
                            pass

        results = await asyncio.gather(*[check(c) for c in configs])
        return [c for c in results if c is not None]

    async def run(self, configs: List[ParsedConfig],
                  progress_cb: Optional[Callable[[dict], None]] = None) -> List[ParsedConfig]:
        """Screen everything cheaply, then measure the few that answered.

        ## Why two passes

        One pass has to pick a single concurrency, and the two jobs it is doing
        want opposite values. Proving that 7000 mostly-dead endpoints are dead
        is almost entirely waiting — the cost of a blackholed address is a
        timeout, not a core — so it wants to run wide. Measuring what a live
        server's round trip really is wants to run narrow, because a probe that
        queues behind other probes measures this machine's load, not the
        network's.

        At one number, whichever is chosen is wrong for half the work: wide
        gives quick cycles and meaningless pings, narrow gives honest pings and
        a cycle that never finishes. So the endpoints are screened wide with a
        single probe and nothing else, and only the few hundred that answered
        are measured narrow — samples, censorship check and exit lookup all
        happen there, on a list small enough to afford them.
        """
        self._progress_cb = progress_cb
        self._last_emit = 0.0
        self.filtered_out = []

        # One disjoint port range per concurrently running xray, handed out and
        # handed back. This *is* the process limit — see [_run_batch].
        self._ports = asyncio.Queue()
        for i in range(max(1, self.parallel_batches)):
            self._ports.put_nowait(self.base_port + i * (self.batch_size + 5))

        # -- pass 1: screening ------------------------------------------------
        self._begin("screen", len(configs))
        self._measure_sem = asyncio.Semaphore(self.screen_concurrency)
        answered = await self._sweep(configs)
        first = len(answered)

        # Retry the ones with a track record. A node that worked four minutes
        # ago and did not answer just now is far more likely to be having a
        # moment than to have died, and leaving it out costs a server from the
        # published list for a whole cycle.
        for _ in range(self.screen_retries):
            got = {id(c) for c in answered}
            again = [c for c in configs
                     if id(c) not in got and c.extra.get("priority")]
            if not again:
                break
            self._total += len(again)
            answered = answered + await self._sweep(again)

        if len(answered) != first:
            log.info("screening: %d of %d endpoints answered (%d of them on a "
                     "retry of known-good ones)",
                     len(answered), len(configs), len(answered) - first)
        else:
            log.info("screening: %d of %d endpoints answered",
                     len(answered), len(configs))

        # -- pass 2: measuring ------------------------------------------------
        self._begin("measure", len(answered))
        self._measure_sem = asyncio.Semaphore(self.measure_concurrency)
        alive = await self._sweep(answered)

        self._emit_progress(force=True)
        alive.sort(key=lambda c: c.ping)
        return alive

    def _begin(self, mode: str, total: int) -> None:
        """Reset the live counters for a pass."""
        self._mode = mode
        self._total = total
        self._tested = 0
        self._alive = 0
        self._recent = []

    async def _sweep(self, configs: List[ParsedConfig]) -> List[ParsedConfig]:
        """Put every config through a batched xray once, in the current mode."""
        if not configs:
            return []
        batches = [configs[i:i + self.batch_size]
                   for i in range(0, len(configs), self.batch_size)]
        # return_exceptions=True so one batch blowing up (e.g. the OS refusing a
        # new xray process under load) loses only that batch, not the whole
        # cycle. The good batches still publish.
        outcomes = await asyncio.gather(
            *[self._run_batch(b) for b in batches], return_exceptions=True)
        out: List[ParsedConfig] = []
        for o in outcomes:
            if isinstance(o, BaseException):
                log.warning("a test batch failed (skipped): %r", o)
            elif o:
                out.extend(o)
        return out

    def _record(self, result: Optional[ParsedConfig]) -> None:
        """Update live counters after a single config has been probed."""
        self._tested += 1
        if result is not None:
            self._alive += 1
            self._recent.append({
                "address": result.address,
                "port": result.port,
                "protocol": result.protocol,
                "ping": result.ping,
            })
            self._recent = self._recent[-30:]  # keep the tail only
        self._emit_progress()

    def _emit_progress(self, force: bool = False) -> None:
        if self._progress_cb is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < 0.8:
            return  # throttle to ~1 write/sec so we don't thrash the disk
        self._last_emit = now
        try:
            self._progress_cb({
                "stage": self._mode,
                "tested": self._tested,
                "total": self._total,
                "alive": self._alive,
                "recent": list(self._recent),
            })
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    async def sift(self, configs: List[ParsedConfig]
                   ) -> "tuple[List[ParsedConfig], List[ParsedConfig]]":
        """Split the pool into what this xray build will load, and what it won't.

        ## Why this exists

        Free lists carry configs this build cannot parse - a cipher it dropped,
        a transport it renamed, a field that moved. They are a small minority,
        about one in twenty, but they are poison in a batch: xray refuses the
        whole file, so 63 good configs go down with one bad one and the batch
        splits, and splits again, and each half pays a process start and a
        readiness deadline before failing the same way.

        Measured before this: 600 configs took 276 batch starts instead of 10,
        and the screening pass averaged 34 probes in flight against a limit of
        256 - almost all of the time was spent starting xray processes that were
        never going to run, on a machine sitting at a quarter of one core.

        The fix is that xray already knows, and says so. `run -test` validates a
        config and exits without binding anything, and the error names the
        outbound: `failed to build outbound config with tag out12`. So drop
        number 12, ask again, and repeat - three or four cheap validations per
        chunk instead of a tree of real process starts. Halving is kept only for
        the case where the message does not name a tag.
        """
        if not configs:
            return [], []
        size = max(1, self.sift_chunk)
        chunks = [configs[i:i + size] for i in range(0, len(configs), size)]
        sem = asyncio.Semaphore(max(1, self.sift_concurrency))

        async def one(chunk):
            async with sem:
                return await self._sift_chunk(list(chunk))

        results = await asyncio.gather(*[one(c) for c in chunks])
        good = [c for g, _ in results for c in g]
        bad = [c for _, b in results for c in b]
        return good, bad

    async def _sift_chunk(self, chunk: List[ParsedConfig]
                          ) -> "tuple[List[ParsedConfig], List[ParsedConfig]]":
        bad: List[ParsedConfig] = []
        while chunk:
            path = self._write_batch_config(chunk, self.base_port)
            try:
                code, err = await self._xray_test(path)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if code == 0:
                return chunk, bad
            match = _BAD_TAG.search(err)
            if match is None or int(match.group(1)) >= len(chunk):
                # xray would not say which one. Fall back to halving, which is
                # now confined to the handful of cases that need it.
                if len(chunk) == 1:
                    return [], bad + chunk
                mid = len(chunk) // 2
                left, right = await asyncio.gather(
                    self._sift_chunk(chunk[:mid]),
                    self._sift_chunk(chunk[mid:]))
                return left[0] + right[0], bad + left[1] + right[1]
            bad.append(chunk.pop(int(match.group(1))))
        return chunk, bad

    async def _xray_test(self, path: str) -> "tuple[int, str]":
        """Validate a config file without starting anything. (code, output)"""
        proc = await asyncio.create_subprocess_exec(
            self.xray_path, "run", "-test", "-c", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return 1, ""
        return proc.returncode or 0, out.decode("utf-8", "replace")

    # ------------------------------------------------------------------ #
    async def _run_batch(self, batch: List[ParsedConfig]) -> List[ParsedConfig]:
        """One xray process exposing one SOCKS inbound per config in `batch`.

        ## The port range is the process limit

        Every xray started here holds a range from `_ports` for exactly as long
        as it runs, so the number of live processes cannot exceed the number of
        ranges — `parallel_batches`, which is what that setting has always
        claimed to mean.

        It did not mean that before. The limit was a semaphore held by the
        *caller*, and the split-and-retry path at the bottom of this method
        recursed underneath it: a batch of 100 that failed to start became 2,
        then 4, then 8 concurrent xray processes, none of them counted against
        anything. On a 2-core VPS that is self-sustaining — the uncounted
        processes make the next batch miss its startup deadline, which splits
        that one too. Measured on the live server before this change: 239 xray
        processes against a configured limit of 10, and 4 configs surviving a
        cycle out of 7107.

        The range is released *before* recursing, so a split never waits on a
        slot its own parent is holding.
        """
        if not batch:
            return []

        port_base = await self._ports.get()
        config_path = self._write_batch_config(batch, port_base)
        proc = None
        died = False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.xray_path, "run", "-c", config_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ready = await self._wait_ready(port_base, proc, len(batch))
            if ready == "ok":
                tested = await asyncio.gather(
                    *[self._test_one(port_base + i, cfg)
                      for i, cfg in enumerate(batch)])
                return [c for c in tested if c is not None]
            died = (ready == "died")
            if not died:
                # Running, just not listening yet: the machine is busy, the
                # config is not broken. Splitting here is exactly what caused
                # the meltdown above, so do not — the next cycle retries it.
                log.warning("xray did not come up in time for %d configs "
                            "(busy — batch skipped, not split)", len(batch))
        finally:
            await self._stop(proc)
            try:
                os.unlink(config_path)
            except OSError:
                pass
            self._ports.put_nowait(port_base)

        # xray rejected the config outright, which means one or more links in
        # this batch are unparseable by this build. Halve it to isolate them, so
        # a single bad config never costs a batch of good ones.
        if not died or len(batch) == 1:
            return []
        mid = len(batch) // 2
        left, right = await asyncio.gather(
            self._run_batch(batch[:mid]),
            self._run_batch(batch[mid:]),
        )
        return left + right

    async def _stop(self, proc) -> None:
        """Terminate, and be sure it is gone before its ports are handed on."""
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            return
        # Wait again after the kill. Without this the port range returns to the
        # pool while the old process may still hold its listeners, and the next
        # xray fails to bind — which is indistinguishable, from the outside,
        # from a batch full of bad configs.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("an xray process ignored SIGKILL; its ports are in use")

    def _write_batch_config(self, batch: List[ParsedConfig], port_base: int) -> str:
        inbounds, outbounds, rules = [], [], []
        for i, cfg in enumerate(batch):
            in_tag, out_tag = f"in{i}", f"out{i}"
            inbounds.append({
                "tag": in_tag,
                "listen": "127.0.0.1",
                "port": port_base + i,
                "protocol": "socks",
                "settings": {"udp": False, "auth": "noauth"},
                "sniffing": {"enabled": False},
            })
            ob = dict(cfg.outbound)
            ob["tag"] = out_tag
            outbounds.append(ob)
            rules.append({"type": "field", "inboundTag": [in_tag], "outboundTag": out_tag})

        xray_cfg = {
            "log": {"loglevel": "none"},
            "inbounds": inbounds,
            "outbounds": outbounds,
            "routing": {"domainStrategy": "AsIs", "rules": rules},
        }
        fd, path = tempfile.mkstemp(prefix="zonevpn_xray_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(xray_cfg, fh)
        return path

    async def _wait_ready(self, port_base: int, proc, size: int) -> str:
        """Wait for the first inbound to accept. Returns "ok", "died" or "busy".

        The caller needs the reason, not a boolean. A process that *exited*
        rejected its config, and halving the batch will find the culprit. A
        process that is still running simply has not been scheduled yet, and
        halving that one adds load to a machine that already has too much.

        The deadline scales with the batch because the startup cost does: xray
        binds one listener per config in it.
        """
        deadline = time.monotonic() + 3.0 + 0.05 * size
        while time.monotonic() < deadline:
            if proc.returncode is not None:  # xray died (bad config) -> stop early
                return "died"
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port_base), timeout=0.4
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return "ok"
            except Exception:
                await asyncio.sleep(0.1)
        return "died" if proc.returncode is not None else "busy"

    async def _test_one(self, port: int, cfg: ParsedConfig) -> Optional[ParsedConfig]:
        # Global throttle. In the screening pass this is wide (the work is
        # waiting); in the measuring pass it is narrow, so that each measured
        # ping reflects the server's real latency and not our own load.
        assert self._measure_sem is not None
        async with self._measure_sem:
            if self._mode == "screen":
                return await self._screen_one(port, cfg)
            return await self._measure_one(port, cfg)

    async def _screen_one(self, port: int, cfg: ParsedConfig) -> Optional[ParsedConfig]:
        """Does anything at all come back through this tunnel?

        One probe, a generous timeout, nothing else. The only question here is
        whether this endpoint is worth measuring properly, and the answer is
        wrong far more often for being impatient than for being slow: a node
        that needs four seconds for a cold TLS handshake from Iran can still be
        a 120 ms server once the tunnel is up. Whatever delay this pass sees is
        thrown away — it was taken under deliberate contention.
        """
        connector = None
        result: Optional[ParsedConfig] = None
        try:
            connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}")
            timeout = aiohttp.ClientTimeout(total=self.screen_timeout)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(self.test_url, allow_redirects=False) as resp:
                    await resp.read()
                    if resp.status in self.expected:
                        result = cfg
        except Exception:
            result = None
        finally:
            if connector is not None:
                await connector.close()
            self._record(result)
        return result

    async def _measure_one(self, port: int, cfg: ParsedConfig) -> Optional[ParsedConfig]:
        connector = None
        result: Optional[ParsedConfig] = None
        try:
            connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}")
            timeout = aiohttp.ClientTimeout(total=self.probe_timeout)
            best: Optional[int] = None
            ok = True
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # Warm-up, deliberately not measured. This is a fresh xray and a
                # fresh tunnel: the first request pays for the TCP connect, the
                # TLS handshake with the node, and the node's own connection
                # onwards. Ranking servers on that number ranks them by distance
                # twice, and it is the number the old single-pass tester was
                # publishing.
                async with session.get(
                        self.test_url, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=self.screen_timeout)
                ) as resp:
                    await resp.read()
                    if resp.status not in self.expected:
                        ok = False

                if ok:
                    for _ in range(self.ping_samples):
                        start = time.monotonic()
                        async with session.get(self.test_url, allow_redirects=False) as resp:
                            await resp.read()
                            if resp.status not in self.expected:
                                ok = False  # wrong response -> not usable
                                break
                        ping = int((time.monotonic() - start) * 1000)
                        if best is None or ping < best:
                            best = ping

                usable = ok and best is not None and 0 < best <= self.max_ping

                # Order matters: the cheap latency probe has already ruled
                # out most configs, so only the survivors pay for the rest.
                if usable and self.require_censored:
                    if not await self._passes_filter(session):
                        # Healthy and quick, and unable to reach anything
                        # this network blocks — so it is not a way out of
                        # it. Set aside rather than discarded: if *every*
                        # config lands here then the check itself is what
                        # is broken, and the runner falls back to this list
                        # instead of publishing nothing.
                        cfg.ping = best
                        self.filtered_out.append(cfg)
                        usable = False

                if usable and self.min_kbps > 0:
                    kbps = await self._measure_kbps(session)
                    cfg.extra["kbps"] = int(kbps)
                    usable = kbps >= self.min_kbps

                if usable:
                    cfg.ping = best
                    # Reuse the same tunnel for the real exit IP/country.
                    if self.geo_via_tunnel:
                        await self._annotate_exit(session, cfg)
                    result = cfg
        except Exception:
            result = None
        finally:
            if connector is not None:
                await connector.close()
            self._record(result)
        return result


    async def _passes_filter(self, session) -> bool:
        """True as soon as one blocked destination answers through the tunnel.

        Raced rather than tried in turn: a dead target must not spend the whole
        budget, and the first success is all the evidence there is to get.
        """
        if not self.censored_urls:
            return True

        async def probe(url: str) -> bool:
            try:
                timeout = aiohttp.ClientTimeout(total=self.censored_timeout)
                async with session.get(url, allow_redirects=False,
                                       timeout=timeout) as resp:
                    # Any answer at all means bytes crossed the tunnel; a 403
                    # from a site that dislikes our user agent still proves it.
                    await resp.read()
                    return True
            except Exception:
                return False

        tasks = [asyncio.ensure_future(probe(u)) for u in self.censored_urls]
        try:
            for fut in asyncio.as_completed(tasks):
                if await fut:
                    return True
            return False
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _measure_kbps(self, session) -> float:
        """Rough download rate through the tunnel, in kilobytes per second."""
        try:
            timeout = aiohttp.ClientTimeout(total=self.throughput_timeout)
            start = time.monotonic()
            read = 0
            async with session.get(self.throughput_url, allow_redirects=True,
                                   timeout=timeout) as resp:
                async for chunk in resp.content.iter_chunked(16384):
                    read += len(chunk)
                    if read >= self.throughput_bytes:
                        break
            elapsed = max(1e-3, time.monotonic() - start)
            return (read / 1024.0) / elapsed
        except Exception:
            return 0.0

    async def _annotate_exit(self, session, cfg: ParsedConfig) -> None:
        """Best-effort: read the true egress IP + country through the tunnel.

        Cloudflare's trace gives both IP and country directly. If it's
        unreachable from this exit, fall back to a plain IP echo so the runner
        can still geolocate the country (better than an unknown flag).
        """
        ip = loc = ""
        try:
            async with session.get(self.trace_url, allow_redirects=False) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    for line in text.splitlines():
                        if line.startswith("ip="):
                            ip = line[3:].strip()
                        elif line.startswith("loc="):
                            loc = line[4:].strip()
        except Exception:
            pass
        if not ip:
            try:
                async with session.get(self.ip_echo_url, allow_redirects=False) as resp:
                    if resp.status == 200:
                        ip = (await resp.text()).strip()
            except Exception:
                pass
        if ip and ("." in ip or ":" in ip) and len(ip) <= 45:
            cfg.exit_ip = ip
        if len(loc) == 2 and loc.isalpha():
            cfg.country = loc.upper()  # overrides the misleading address-based geo
