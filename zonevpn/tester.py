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
import tempfile
import time
from typing import Callable, List, Optional

import aiohttp
from aiohttp_socks import ProxyConnector

from .links import ParsedConfig

log = logging.getLogger("zonevpn.tester")


class Tester:
    def __init__(self, xray_path: str, cfg: dict):
        self.xray_path = xray_path
        self.test_url: str = cfg.get("test_url", "http://cp.cloudflare.com/generate_204")
        self.expected = set(cfg.get("expected_status", [204, 200]))
        self.timeout: float = float(cfg.get("timeout", 6))
        self.batch_size: int = int(cfg.get("batch_size", 100))
        self.parallel_batches: int = int(cfg.get("parallel_batches", 4))
        self.base_port: int = int(cfg.get("base_port", 20000))
        self.max_ping: int = int(cfg.get("max_ping", 3000))

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

        # Bound the wait per probe so a hanging server can't stall the cycle:
        # nothing slower than max_ping can win anyway, so don't wait much past it.
        self.probe_timeout: float = min(
            self.timeout, self.max_ping / 1000.0 + 1.0
        )

        # Created in run(), bound to the running event loop.
        self._measure_sem: Optional[asyncio.Semaphore] = None

        # Live progress (for the dashboard). Reset at the start of every run().
        self._progress_cb: Optional[Callable[[dict], None]] = None
        self._total = 0
        self._tested = 0
        self._alive = 0
        self._recent: List[dict] = []
        self._last_emit = 0.0

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
                try:
                    fut = asyncio.open_connection(cfg.address, cfg.port)
                    _, writer = await asyncio.wait_for(fut, timeout=timeout)
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
        # Global cap on simultaneous delay probes (accuracy, see __init__).
        self._measure_sem = asyncio.Semaphore(self.measure_concurrency)

        # Reset live-progress state for this run.
        self._progress_cb = progress_cb
        self._total = len(configs)
        self._tested = 0
        self._alive = 0
        self._recent = []
        self._last_emit = 0.0

        batches = [configs[i:i + self.batch_size] for i in range(0, len(configs), self.batch_size)]
        sem = asyncio.Semaphore(self.parallel_batches)
        results: List[List[ParsedConfig]] = [None] * len(batches)  # type: ignore

        async def worker(slot: int, idx: int, batch: List[ParsedConfig]):
            async with sem:
                # disjoint port range per slot so parallel batches never collide
                port_base = self.base_port + slot * (self.batch_size + 5)
                results[idx] = await self._run_batch(batch, port_base)

        tasks = []
        for idx, batch in enumerate(batches):
            slot = idx % self.parallel_batches
            tasks.append(asyncio.create_task(worker(slot, idx, batch)))
        # return_exceptions=True so one batch blowing up (e.g. the OS refusing a
        # new xray process under load) loses only that batch, not the whole
        # cycle. The good batches still publish.
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for o in outcomes:
            if isinstance(o, BaseException):
                log.warning("a test batch failed (skipped): %r", o)

        self._emit_progress(force=True)
        alive = [c for sub in results if sub for c in sub]
        alive.sort(key=lambda c: c.ping)
        return alive

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
                "tested": self._tested,
                "total": self._total,
                "alive": self._alive,
                "recent": list(self._recent),
            })
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    async def _run_batch(self, batch: List[ParsedConfig], port_base: int) -> List[ParsedConfig]:
        if not batch:
            return []

        config_path = self._write_batch_config(batch, port_base)
        proc = None
        started = False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.xray_path, "run", "-c", config_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            started = await self._wait_ready(port_base, proc)
            if started:
                tasks = [self._test_one(port_base + i, cfg) for i, cfg in enumerate(batch)]
                tested = await asyncio.gather(*tasks)
                return [c for c in tested if c is not None]
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            try:
                os.unlink(config_path)
            except OSError:
                pass

        # Startup failed: one (or more) configs in this batch are unparseable by
        # xray. Isolate them by splitting the batch and retrying each half, so a
        # single bad config never wastes a whole batch of good ones. The halves
        # run concurrently on disjoint port ranges (right offset by mid) so deep
        # splits stay fast instead of adding up serially.
        if len(batch) == 1:
            return []  # the lone config can't start -> drop it
        mid = len(batch) // 2
        left, right = await asyncio.gather(
            self._run_batch(batch[:mid], port_base),
            self._run_batch(batch[mid:], port_base + mid),
        )
        return left + right

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

    async def _wait_ready(self, port_base: int, proc) -> bool:
        """Wait until the first inbound accepts a TCP connection (xray is up).

        Fails fast: if xray rejected the config it exits within ~200ms, so we
        detect the dead process immediately instead of polling for the full
        deadline. This keeps the split-on-failure path cheap.
        """
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if proc.returncode is not None:  # xray died (bad config) -> stop early
                return False
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port_base), timeout=0.4
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return True
            except Exception:
                await asyncio.sleep(0.1)
        return False

    async def _test_one(self, port: int, cfg: ParsedConfig) -> Optional[ParsedConfig]:
        # Global throttle: only `measure_concurrency` probes run at once, so each
        # measured ping reflects the server's real latency, not server load.
        assert self._measure_sem is not None
        async with self._measure_sem:
            connector = None
            result: Optional[ParsedConfig] = None
            try:
                connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}")
                timeout = aiohttp.ClientTimeout(total=self.probe_timeout)
                best: Optional[int] = None
                ok = True
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
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
                if ok and best is not None and 0 < best <= self.max_ping:
                    cfg.ping = best
                    result = cfg
            except Exception:
                result = None
            finally:
                if connector is not None:
                    await connector.close()
                self._record(result)
            return result
