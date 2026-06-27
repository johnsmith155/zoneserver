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
from typing import List, Optional

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

    async def run(self, configs: List[ParsedConfig]) -> List[ParsedConfig]:
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
        await asyncio.gather(*tasks)

        alive = [c for sub in results if sub for c in sub]
        alive.sort(key=lambda c: c.ping)
        return alive

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
        connector = None
        try:
            connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}")
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            start = time.monotonic()
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(self.test_url, allow_redirects=False) as resp:
                    await resp.read()
                    if resp.status not in self.expected:
                        return None
            ping = int((time.monotonic() - start) * 1000)
            if ping <= 0 or ping > self.max_ping:
                return None
            cfg.ping = ping
            return cfg
        except Exception:
            return None
        finally:
            if connector is not None:
                await connector.close()
