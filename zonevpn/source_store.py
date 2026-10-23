"""Dashboard sources, read afresh each cycle; never rewrite the main config."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import tempfile
from urllib.parse import urlsplit, urlunsplit

from . import state

MAX_CUSTOM = 64
MAX_SOURCES = 128
MAX_URL = 2048
MAX_STORE_BYTES = 256 * 1024


def public_ip(host: str) -> bool:
    ip = ipaddress.ip_address(host)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def normalize_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter an HTTP or HTTPS source URL.")
    value = value.strip()
    if not value or len(value) > MAX_URL or any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("Invalid URL (maximum 2048 characters, no spaces).")
    try:
        u = urlsplit(value)
        host, port = u.hostname, u.port
        if u.scheme.lower() not in ("http", "https") or not host or u.username is not None or u.password is not None:
            raise ValueError()
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.endswith((".localhost", ".local", ".internal")):
                raise ValueError()
        else:
            if not public_ip(host):
                raise ValueError()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError("Use a public HTTP(S) URL without embedded username/password.") from None
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if u.scheme.lower() == "https" else 80):
        authority += f":{port}"
    return urlunsplit((u.scheme.lower(), authority, u.path or "/", u.query, ""))


def source_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def read_custom() -> list[str]:
    path = state.STATE_DIR / "sources.json"
    try:
        with path.open("rb") as f:
            raw = f.read(MAX_STORE_BYTES + 1)
    except FileNotFoundError:
        return []
    if len(raw) > MAX_STORE_BYTES:
        raise ValueError("Saved sources file is too large; restore it before editing.")
    try:
        items = json.loads(raw)
        if not isinstance(items, list) or len(items) > MAX_CUSTOM:
            raise ValueError()
        return list(dict.fromkeys(normalize_url(u) for u in items))
    except (ValueError, TypeError):
        raise ValueError("Saved sources file is invalid; restore it before editing.") from None


def write_custom(items: list[str]) -> None:
    # Only dashboard handlers write this file. Their read/modify/write section
    # contains no await, so requests cannot interleave. Readers see old or new.
    state.ensure_dir()
    fd, name = tempfile.mkstemp(prefix=".sources-", dir=state.STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, state.STATE_DIR / "sources.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def configured_urls(configured) -> list[str]:
    result = []
    if not isinstance(configured, list):
        return result
    for value in configured[:MAX_SOURCES]:
        try:
            url = normalize_url(value)
        except ValueError:
            continue
        if url not in result:
            result.append(url)
    return result


def effective(configured, custom: list[str]) -> list[str]:
    return list(dict.fromkeys(configured_urls(configured) + custom))[:MAX_SOURCES]
