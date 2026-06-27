"""Fetch raw config text from the configured sources and turn it into ParsedConfig objects."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import List

import aiohttp

from .links import ParsedConfig, dedup_key, parse_link

log = logging.getLogger("zonevpn.sources")

_PREFIXES = ("vmess://", "vless://", "trojan://", "ss://")


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
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            if resp.status != 200:
                log.warning("source %s -> HTTP %s", url, resp.status)
                return []
            text = await resp.text(errors="replace")
    except Exception as exc:
        log.warning("source %s failed: %s", url, exc)
        return []

    text = _maybe_base64_subscription(text)
    links = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(_PREFIXES)]
    log.info("source %s -> %d raw links", url, len(links))
    return links


async def collect(sources: List[str]) -> List[ParsedConfig]:
    """Fetch every source, parse, and de-duplicate."""
    headers = {"User-Agent": "Mozilla/5.0 (ZoneVPN config collector)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        results = await asyncio.gather(*[_fetch_one(session, u) for u in sources])

    seen: set[str] = set()
    parsed: List[ParsedConfig] = []
    raw_count = 0
    for links in results:
        for link in links:
            raw_count += 1
            cfg = parse_link(link)
            if cfg is None:
                continue
            key = dedup_key(cfg)
            if key in seen:
                continue
            seen.add(key)
            parsed.append(cfg)

    log.info("collected %d raw links -> %d unique parseable configs", raw_count, len(parsed))
    return parsed
