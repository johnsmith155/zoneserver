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
import socket
from typing import Dict, List, Optional

log = logging.getLogger("zonevpn.geo")

_UNKNOWN_FLAG = "🏴"


def flag_emoji(cc: str) -> str:
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return _UNKNOWN_FLAG
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc)


class GeoResolver:
    def __init__(self, mmdb_path: Optional[str] = None):
        self._reader = None
        self._ip_cache: Dict[str, str] = {}      # host -> ip
        self._cc_cache: Dict[str, str] = {}       # ip   -> country code
        if mmdb_path:
            try:
                import geoip2.database  # type: ignore
                self._reader = geoip2.database.Reader(mmdb_path)
                log.info("GeoIP database loaded: %s", mmdb_path)
            except FileNotFoundError:
                log.warning("GeoIP db not found at %s; will use ip-api fallback", mmdb_path)
            except Exception as exc:
                log.warning("GeoIP db could not be loaded (%s); using ip-api fallback", exc)

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
