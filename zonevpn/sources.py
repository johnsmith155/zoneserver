"""Fetch raw config text from the configured sources and turn it into ParsedConfig objects."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Dict, List, Tuple

import aiohttp

from .links import ParsedConfig, dedup_key, parse_link

log = logging.getLogger("zonevpn.sources")

_PREFIXES = ("vmess://", "vless://", "trojan://", "ss://")

# url -> (validator, links). See [_fetch_one].
_CACHE: Dict[str, Tuple[Dict[str, str], List[str]]] = {}


def short_name(url: str) -> str:
    """A readable id for a source, for the per-source yield log.

    `https://raw.githubusercontent.com/owner/repo/main/path/file.txt`
    becomes `owner/repo:file.txt`, which is short enough to line up in a log
    and specific enough to tell two files in one repo apart.
    """
    body = url.split("://", 1)[-1]
    parts = [p for p in body.split("/") if p]
    if "raw.githubusercontent.com" in body and len(parts) >= 3:
        owner, repo = parts[1], parts[2]
        return f"{owner}/{repo}:{parts[-1]}"
    return parts[-1] if parts else url


def _maybe_base64_subscription(text: str) -> str:
    """Many sub files are a single base64 blob. If the body has no scheme but
    decodes to one, treat the decoded text as the real content."""
    stripped = "".join(text.split())
    if any(p in text for p in _PREFIXES):
        return text
    try:
        decoded = base64.b64decode(stripped + "=" * (-len(stripped) % 4)).decode("utf-8", "replace")
        if any(p in decoded for p in _PREFIXES):
            return decoded
    except Exception:
        pass
    return text


async def _fetch_one(session: aiohttp.ClientSession, url: str) -> List[str]:
    """Fetch one source, re-using the last body when it has not changed.

    These files are several megabytes each and a cycle runs every five minutes,
    while the repositories behind them update every ten to thirty. Downloading
    them every time costs the VPS gigabytes a day to receive bytes it already
    has. Every one of them is served by a CDN that answers `304 Not Modified`
    to a conditional request, so ask conditionally and keep the parsed links;
    the cache lives in the process, so a restart simply fetches once.
    """
    cached = _CACHE.get(url)
    headers = dict(cached[0]) if cached else {}

    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=45)) as resp:
            if resp.status == 304 and cached:
                log.info("source %s -> unchanged (%d links, cached)",
                         url, len(cached[1]))
                return cached[1]
            if resp.status != 200:
                if cached:
                    # A source having a bad minute should not shrink the pool.
                    log.warning("source %s -> HTTP %s (using last good copy)",
                                url, resp.status)
                    return cached[1]
                log.warning("source %s -> HTTP %s", url, resp.status)
                return []
            text = await resp.text(errors="replace")
            validator = {}
            if resp.headers.get("ETag"):
                validator["If-None-Match"] = resp.headers["ETag"]
            if resp.headers.get("Last-Modified"):
                validator["If-Modified-Since"] = resp.headers["Last-Modified"]
    except Exception as exc:
        if cached:
            log.warning("source %s failed: %s (using last good copy)", url, exc)
            return cached[1]
        log.warning("source %s failed: %s", url, exc)
        return []

    text = _maybe_base64_subscription(text)
    links = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(_PREFIXES)]
    log.info("source %s -> %d raw links", url, len(links))
    if validator:
        _CACHE[url] = (validator, links)
    return links


async def collect(sources: List[str]) -> List[ParsedConfig]:
    """Fetch every source, parse, and de-duplicate.

    Each surviving config remembers which source it came from (`extra["src"]`)
    so the runner can report what each one is actually worth. A config found in
    several sources is credited to the first that listed it — these files copy
    from each other constantly, and crediting all of them would make every
    aggregator look equally productive.
    """
    headers = {"User-Agent": "Mozilla/5.0 (ZoneVPN config collector)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        results = await asyncio.gather(*[_fetch_one(session, u) for u in sources])

    seen: set[str] = set()
    parsed: List[ParsedConfig] = []
    raw_count = 0
    for url, links in zip(sources, results):
        src = short_name(url)
        for link in links:
            raw_count += 1
            cfg = parse_link(link)
            if cfg is None:
                continue
            key = dedup_key(cfg)
            if key in seen:
                continue
            seen.add(key)
            cfg.extra["src"] = src
            parsed.append(cfg)

    log.info("collected %d raw links -> %d unique parseable configs", raw_count, len(parsed))
    return parsed
