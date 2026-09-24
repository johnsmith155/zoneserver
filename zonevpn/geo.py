"""Resolve a server host to an ISO country code and a flag emoji.

Strategy (in order, all best-effort):
  1. Local MaxMind/DB-IP country mmdb (fast, no rate limit) if present.
  2. ip-api.com batch endpoint (free, no key, 100 ips/request).
Resolution + lookups are cached for the lifetime of the process and done off the
event loop so they never stall config testing.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shutil
import socket
import time
import urllib.request
from typing import Dict, List, Optional

log = logging.getLogger("zonevpn.geo")

_UNKNOWN_FLAG = "🏴"


def _download_db(url: str, path: str) -> None:
    """Fetch a country mmdb to [path], replacing it only if the new one works."""
    import geoip2.database  # type: ignore

    tmp = path + ".new"
    req = urllib.request.Request(url, headers={"User-Agent": "zoneserver/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    try:
        with geoip2.database.Reader(tmp) as reader:
            # A database that cannot place a plain German hosting range is not
            # one to label a fleet with. Not an anycast address: this build
            # names those by network ("GOOGLE" for 8.8.8.8, "CLOUDFLARE" for
            # 1.1.1.1), which is where labels like CLOUDFRONT come from.
            if reader.country("5.9.0.1").country.iso_code != "DE":
                raise ValueError("unexpected answer for 5.9.0.1 (Hetzner)")
    except Exception:
        os.remove(tmp)
        raise
    os.replace(tmp, path)


def flag_emoji(cc: str) -> str:
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return _UNKNOWN_FLAG
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc)


class GeoResolver:
    def __init__(self, mmdb_path: Optional[str] = None):
        self._reader = None
        self._path = mmdb_path
        self._ip_cache: Dict[str, str] = {}      # host -> ip
        self._cc_cache: Dict[str, str] = {}       # ip   -> country code
        self._last_try = 0.0
        self._open()

    def _open(self) -> None:
        if not self._path:
            return
        try:
            import geoip2.database  # type: ignore
            self._reader = geoip2.database.Reader(self._path)
            log.info("GeoIP database loaded: %s", self._path)
        except FileNotFoundError:
            log.warning("GeoIP db not found at %s; will use ip-api fallback", self._path)
        except Exception as exc:
            log.warning("GeoIP db could not be loaded (%s); using ip-api fallback", exc)

    # Weekly, like the release it comes from; retried at most this often.
    _MAX_AGE = 7 * 24 * 3600
    _RETRY_EVERY = 6 * 3600

    async def refresh_if_stale(self, url: Optional[str]) -> bool:
        """Replace the database with the latest release once it is a week old.

        The labels this writes are the ones the app checks against: once
        connected, the app asks MaxMind-backed services where the tunnel
        really exits, and a label from a stale copy disagrees with them
        wherever an address has moved since — one country on the card, another
        on the status line. install.sh fetched this file once; the copy in use
        on 2026-09-24 dated from 2026-06-30 and had never been updated. The
        release it comes from is rebuilt weekly.
        """
        if not self._path or not url:
            return False
        now = time.time()
        try:
            age = now - os.path.getmtime(self._path)
        except OSError:
            age = float("inf")
        if age < self._MAX_AGE or now - self._last_try < self._RETRY_EVERY:
            return False
        self._last_try = now
        try:
            await asyncio.to_thread(_download_db, url, self._path)
        except Exception as exc:
            log.warning("GeoIP db refresh failed (keeping the old one): %s", exc)
            return False
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
            self._reader = None
        self._open()
        # Every answer so far came from the old copy.
        self._cc_cache.clear()
        self._ip_cache.clear()
        log.info("GeoIP db refreshed from %s", url)
        return True

    async def resolve_ip(self, host: str) -> Optional[str]:
        if not host:
            return None
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        if host in self._ip_cache:
            return self._ip_cache[host]
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            ip = infos[0][4][0]
            self._ip_cache[host] = ip
            return ip
        except Exception:
            self._ip_cache[host] = ""
            return None

    def _mmdb_lookup(self, ip: str) -> Optional[str]:
        if not self._reader:
            return None
        try:
            return self._reader.country(ip).country.iso_code or None
        except Exception:
            return None

    async def annotate(self, hosts: List[str]) -> Dict[str, str]:
        """Return {host: country_code}. Empty string when unknown."""
        ips: Dict[str, str] = {}
        for h in set(hosts):
            ip = await self.resolve_ip(h)
            if ip:
                ips[h] = ip

        result: Dict[str, str] = {}
        need_api: List[str] = []
        for h, ip in ips.items():
            if ip in self._cc_cache:
                result[h] = self._cc_cache[ip]
                continue
            cc = self._mmdb_lookup(ip)
            if cc:
                self._cc_cache[ip] = cc
                result[h] = cc
            else:
                need_api.append(ip)

        if need_api:
            api_cc = await self._ipapi_batch(list(dict.fromkeys(need_api)))
            for h, ip in ips.items():
                if h not in result and ip in api_cc:
                    self._cc_cache[ip] = api_cc[ip]
                    result[h] = api_cc[ip]

        for h in hosts:
            result.setdefault(h, "")
        return result

    async def _ipapi_batch(self, ips: List[str]) -> Dict[str, str]:
        import aiohttp
        out: Dict[str, str] = {}
        url = "http://ip-api.com/batch?fields=countryCode,query,status"
        try:
            async with aiohttp.ClientSession() as session:
                for i in range(0, len(ips), 100):
                    chunk = ips[i:i + 100]
                    async with session.post(
                        url, json=chunk, timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        for row in data:
                            if row.get("status") == "success" and row.get("countryCode"):
                                out[row["query"]] = row["countryCode"]
                    if i + 100 < len(ips):
                        await asyncio.sleep(1.4)  # ip-api: ~45 req/min
        except Exception as exc:
            log.warning("ip-api lookup failed: %s", exc)
        return out
