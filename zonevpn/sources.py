"""Bounded, isolated downloads of public subscription sources."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import json
import logging
import socket
import sys
import time
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.resolver import ThreadedResolver

from . import source_store, state
from .links import ParsedConfig, dedup_key, parse_link

log = logging.getLogger("zonevpn.sources")
_PREFIXES = ("vmess://", "vless://", "trojan://", "ss://")
MAX_BODY = 8 * 1024 * 1024
MAX_LINK = 16 * 1024
MAX_LINKS = 20000
MAX_CONFIGS = 50000
MAX_PARSE_BYTES = 32 * 1024 * 1024
CACHE_BYTES = 32 * 1024 * 1024
CACHE_TTL = 3600
CONCURRENCY = 4
FETCH_TIMEOUT = 45
CYCLE_TIMEOUT = 120
_CACHE = OrderedDict()


def short_name(url: str) -> str:
    u = urlsplit(url)
    parts = [p for p in u.path.split("/") if p]
    if u.hostname == "raw.githubusercontent.com" and len(parts) >= 3:
        return f"{parts[0]}/{parts[1]}:{parts[-1]}"
    return f"{u.hostname}/{parts[-1]}" if parts else str(u.hostname)


class PublicResolver(ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await super().resolve(host, port, family)
        if not records or any(not source_store.public_ip(r["host"]) for r in records):
            raise OSError("Source hostname resolves to a non-public address")
        return records


def _maybe_base64_subscription(text: str) -> str:
    if any(p in text for p in _PREFIXES):
        return text
    stripped = "".join(text.split())
    try:
        decoded = base64.b64decode(stripped + "=" * (-len(stripped) % 4), altchars=b"-_").decode("utf-8", "replace")
        if any(p in decoded for p in _PREFIXES):
            return decoded
    except (ValueError, UnicodeError):
        pass
    return text


def _cache_put(url, validators, links):
    size = sys.getsizeof(links) + sum(sys.getsizeof(s) for s in links)
    _CACHE.pop(url, None)
    if size > CACHE_BYTES:
        return
    while _CACHE and sum(item[3] for item in _CACHE.values()) + size > CACHE_BYTES:
        _CACHE.popitem(last=False)
    _CACHE[url] = (validators, links, time.monotonic(), size)


def _health(health, url, status, message, count=0):
    if health is not None:
        health[source_store.source_id(url)] = {
            "status": status, "message": message, "links": count,
            "checked_at": int(time.time()),
        }


async def _download(session, url, validators):
    current = url
    for hop in range(4):
        # Recheck every redirect, including numeric hosts that bypass DNS.
        current = source_store.normalize_url(current)
        async with session.get(current, headers=validators, allow_redirects=False,
                               auto_decompress=False,
                               timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT, connect=10, sock_read=15)) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                if hop == 3 or not resp.headers.get("Location"):
                    raise ValueError("Too many redirects or missing redirect target")
                current = urljoin(current, resp.headers["Location"])
                continue
            if resp.status == 304:
                return None, {}
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            # Avoid allocating an unbounded decompressed response.
            if resp.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise ValueError("Source ignored request for uncompressed content")
            if resp.content_length is not None and resp.content_length > MAX_BODY:
                raise ValueError("Source exceeds 8 MiB limit")
            body = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                if len(body) + len(chunk) > MAX_BODY:
                    raise ValueError("Source exceeds 8 MiB limit")
                body.extend(chunk)
            headers = {}
            for src, dest in (("ETag", "If-None-Match"), ("Last-Modified", "If-Modified-Since")):
                if resp.headers.get(src):
                    headers[dest] = resp.headers[src]
            return body.decode("utf-8-sig", "replace"), headers
    raise ValueError("Too many redirects")


async def _fetch_one(session, url: str, health=None) -> list[str]:
    cached = _CACHE.get(url)
    if cached and time.monotonic() - cached[2] > CACHE_TTL:
        _CACHE.pop(url, None)
        cached = None
    try:
        text, validators = await asyncio.wait_for(
            _download(session, url, cached[0] if cached else {}), FETCH_TIMEOUT)
        if text is None:
            if not cached:
                raise ValueError("HTTP 304 without a cached copy")
            _cache_put(url, cached[0], cached[1])
            _health(health, url, "ok", "Unchanged", len(cached[1]))
            return cached[1]
        text = _maybe_base64_subscription(text)
        links, limited = [], False
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith(_PREFIXES):
                continue
            if len(line) > MAX_LINK:
                limited = True
                continue
            if len(links) >= MAX_LINKS:
                limited = True
                break
            links.append(line)
        if not links:
            raise ValueError("No supported links found (plain text or base64 required)")
        _cache_put(url, validators, links)
        _health(health, url, "limited" if limited else "ok",
                "Some links exceeded limits" if limited else "Downloaded", len(links))
        log.info("source %s -> %d raw links", short_name(url), len(links))
        return links
    except Exception as exc:
        # Avoid leaking query credentials through aiohttp exception strings.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        if cached:
            _health(health, url, "cached", message + "; using previous download", len(cached[1]))
            log.warning("source %s -> %s (cached)", short_name(url), message)
            return cached[1]
        _health(health, url, "error", message)
        log.warning("source %s -> %s", short_name(url), message)
        return []


async def collect(sources: list[str]) -> list[ParsedConfig]:
    try:
        custom = source_store.read_custom()
    except (OSError, ValueError) as exc:
        log.error("Cannot read custom sources (%s); using configured sources", type(exc).__name__)
        custom = []
    urls = source_store.effective(sources, custom)
    for url in list(_CACHE):
        if url not in urls or time.monotonic() - _CACHE[url][2] > CACHE_TTL:
            _CACHE.pop(url, None)
    health = {source_store.source_id(u): {
        "status": "waiting", "message": "Not reached within this cycle's limits",
        "links": 0, "checked_at": int(time.time()),
    } for u in urls}
    seen, parsed = set(), []
    raw_count = parse_bytes = 0
    deadline = time.monotonic() + CYCLE_TIMEOUT
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=CONCURRENCY,
                                     limit_per_host=CONCURRENCY, family=socket.AF_UNSPEC)
    async with aiohttp.ClientSession(connector=connector, headers={
        "User-Agent": "Mozilla/5.0 (ZoneVPN config collector)", "Accept-Encoding": "identity",
    }, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
        # Process a bounded batch at a time; never hold all downloaded bodies.
        for offset in range(0, len(urls), CONCURRENCY):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(parsed) >= MAX_CONFIGS or parse_bytes >= MAX_PARSE_BYTES:
                log.warning("Source collection reached cycle limits")
                break
            batch = urls[offset:offset + CONCURRENCY]
            tasks = [asyncio.create_task(_fetch_one(session, u, health)) for u in batch]
            try:
                done, pending = await asyncio.wait(tasks, timeout=remaining)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for url, task in zip(batch, tasks):
                    if task not in done or task.cancelled():
                        continue
                    try:
                        items = task.result()
                    except Exception:
                        _health(health, url, "error", "Source processing failed")
                        continue
                    for link in items:
                        if len(parsed) >= MAX_CONFIGS or parse_bytes >= MAX_PARSE_BYTES:
                            break
                        raw_count += 1
                        parse_bytes += len(link)
                        try:
                            cfg = parse_link(link)
                            if cfg is None:
                                continue
                            key = dedup_key(cfg)
                            if key in seen:
                                continue
                            seen.add(key)
                            cfg.extra["src"] = short_name(url)
                            parsed.append(cfg)
                        except Exception:
                            continue
                    await asyncio.sleep(0)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    try:
        state._atomic_write(state.STATE_DIR / "source_health.json", json.dumps(health))
    except OSError:
        log.warning("Could not save source health")
    log.info("collected %d raw links -> %d unique parseable configs", raw_count, len(parsed))
    return parsed
