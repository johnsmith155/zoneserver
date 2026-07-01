"""Parse V2Ray share links into xray outbound objects, and rebuild them with new names.

Supported protocols (all natively testable by xray-core):
  - vmess://
  - vless://
  - trojan://
  - ss://   (shadowsocks)

Protocols xray-core cannot test (hysteria, hysteria2, tuic, ...) are intentionally
skipped so they never end up in the published list.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional


# When True, TLS outbounds include allowInsecure=true. Only works on xray-core
# versions that still support it (removed in recent releases). Set from config.
ALLOW_INSECURE = False


@dataclass
class ParsedConfig:
    protocol: str            # vmess | vless | trojan | shadowsocks
    address: str             # server host (domain or ip)
    port: int
    outbound: dict           # xray outbound object (without "tag")
    raw: str                 # original share link
    name: str = ""           # original remark
    # filled in later by the pipeline:
    ping: int = 0            # real delay through the tunnel (ms)
    tcp_ping: int = -1       # raw TCP connect time to address:port (ms)
    country: str = ""        # ISO-3166 alpha-2, e.g. "DE"
    exit_ip: str = ""        # the REAL egress IP seen through the tunnel
    manual: bool = False     # operator-added (dashboard) -> always kept
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# base64 helpers (free configs are notoriously sloppy with padding / urlsafe)  #
# --------------------------------------------------------------------------- #
def _b64decode(data: str) -> bytes:
    data = data.strip()
    data = data.replace("-", "+").replace("_", "/")
    pad = len(data) % 4
    if pad:
        data += "=" * (4 - pad)
    return base64.b64decode(data)


def _b64decode_str(data: str) -> str:
    return _b64decode(data).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# stream settings (shared by vmess / vless / trojan)                           #
# --------------------------------------------------------------------------- #
def _build_stream(network: str, security: str, params: dict, host_fallback: str) -> dict:
    network = (network or "tcp").lower()
    if network in ("h2", "http"):
        network = "http"
    security = (security or "none").lower()

    stream: dict = {"network": network, "security": security}

    sni = params.get("sni") or params.get("peer") or ""
    host = params.get("host") or ""
    path = params.get("path") or ""
    fp = params.get("fp") or ""

    if network == "ws":
        ws = {"path": path or "/"}
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        service = params.get("serviceName") or params.get("servicename") or path or ""
        stream["grpcSettings"] = {
            "serviceName": service,
            "multiMode": (params.get("mode", "") == "multi"),
        }
    elif network == "http":
        hosts = [h for h in host.split(",") if h] or ([host_fallback] if host_fallback else [])
        stream["httpSettings"] = {"path": path or "/", "host": hosts}
    elif network == "kcp":
        stream["kcpSettings"] = {
            "header": {"type": params.get("headerType", "none")},
            "seed": params.get("seed", ""),
        }
    elif network == "tcp":
        if params.get("headerType") == "http":
            stream["tcpSettings"] = {
                "header": {
                    "type": "http",
                    "request": {
                        "path": [path or "/"],
                        "headers": {"Host": [host] if host else [host_fallback]},
                    },
                }
            }

    if security == "tls":
        # NOTE: 'allowInsecure' was removed in recent xray-core and aborts config
        # loading there. We only add it when ALLOW_INSECURE is on (pin an older
        # xray in that case). With it on, configs using a mismatched/self-signed
        # cert (very common in free lists) still pass and remain usable in apps
        # that support allowInsecure.
        tls = {"serverName": sni or host or host_fallback}
        if ALLOW_INSECURE:
            tls["allowInsecure"] = True
        if fp:
            tls["fingerprint"] = fp
        alpn = params.get("alpn")
        if alpn:
            tls["alpn"] = alpn.split(",")
        stream["tlsSettings"] = tls
    elif security == "reality":
        reality = {
            "serverName": sni or host_fallback,
            "fingerprint": fp or "chrome",
            "publicKey": params.get("pbk", ""),
            "shortId": params.get("sid", ""),
            "spiderX": params.get("spx", ""),
        }
        stream["realitySettings"] = reality

    return stream


# --------------------------------------------------------------------------- #
# per-protocol parsers                                                         #
# --------------------------------------------------------------------------- #
def _parse_vmess(link: str) -> Optional[ParsedConfig]:
    body = link[len("vmess://"):]
    try:
        info = json.loads(_b64decode_str(body))
    except Exception:
        return None

    address = str(info.get("add", "")).strip()
    try:
        port = int(info.get("port"))
    except (TypeError, ValueError):
        return None
    if not address or not port:
        return None

    net = str(info.get("net", "tcp"))
    tls = str(info.get("tls", "")) or "none"
    params = {
        "host": info.get("host", ""),
        "path": info.get("path", ""),
        "sni": info.get("sni", "") or info.get("host", ""),
        "serviceName": info.get("path", "") if net == "grpc" else "",
        "headerType": info.get("type", ""),
        "alpn": info.get("alpn", ""),
        "fp": info.get("fp", ""),
    }
    try:
        aid = int(info.get("aid", 0) or 0)
    except (TypeError, ValueError):
        aid = 0

    outbound = {
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": address,
                "port": port,
                "users": [{
                    "id": str(info.get("id", "")),
                    "alterId": aid,
                    "security": str(info.get("scy", "auto") or "auto"),
                }],
            }]
        },
        "streamSettings": _build_stream(net, tls, params, address),
    }
    return ParsedConfig("vmess", address, port, outbound, link, str(info.get("ps", "")))


def _parse_vless(link: str) -> Optional[ParsedConfig]:
    u = urllib.parse.urlparse(link)
    uuid = urllib.parse.unquote(u.username or "")
    address = u.hostname or ""
    port = u.port or 0
    if not uuid or not address or not port:
        return None
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    net = q.get("type", "tcp")
    security = q.get("security", "none")
    user = {"id": uuid, "encryption": q.get("encryption", "none")}
    flow = q.get("flow")
    if flow:
        user["flow"] = flow

    outbound = {
        "protocol": "vless",
        "settings": {"vnext": [{"address": address, "port": port, "users": [user]}]},
        "streamSettings": _build_stream(net, security, q, address),
    }
    name = urllib.parse.unquote(u.fragment) if u.fragment else ""
    return ParsedConfig("vless", address, port, outbound, link, name)


def _parse_trojan(link: str) -> Optional[ParsedConfig]:
    u = urllib.parse.urlparse(link)
    password = urllib.parse.unquote(u.username or "")
    address = u.hostname or ""
    port = u.port or 0
    if not password or not address or not port:
        return None
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    net = q.get("type", "tcp")
    security = q.get("security", "tls")  # trojan implies tls by default

    outbound = {
        "protocol": "trojan",
        "settings": {"servers": [{"address": address, "port": port, "password": password}]},
        "streamSettings": _build_stream(net, security, q, address),
    }
    name = urllib.parse.unquote(u.fragment) if u.fragment else ""
    return ParsedConfig("trojan", address, port, outbound, link, name)


def _parse_ss(link: str) -> Optional[ParsedConfig]:
    body = link[len("ss://"):]
    name = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        name = urllib.parse.unquote(frag)

    method = password = address = ""
    port = 0

    if "@" in body:
        # ss://base64(method:password)@host:port   (userinfo may or may not be b64)
        userinfo, server = body.rsplit("@", 1)
        userinfo = urllib.parse.unquote(userinfo)
        if ":" not in userinfo:
            try:
                userinfo = _b64decode_str(userinfo)
            except Exception:
                return None
        if ":" not in userinfo or ":" not in server:
            return None
        method, password = userinfo.split(":", 1)
        host, port_s = server.rsplit(":", 1)
        address = host
    else:
        # ss://base64(method:password@host:port)
        try:
            decoded = _b64decode_str(body)
        except Exception:
            return None
        if "@" not in decoded or ":" not in decoded:
            return None
        userinfo, server = decoded.rsplit("@", 1)
        method, password = userinfo.split(":", 1)
        host, port_s = server.rsplit(":", 1)
        address = host

    try:
        port = int(port_s.split("?")[0].split("/")[0])
    except (ValueError, NameError):
        return None
    if not method or not address or not port:
        return None

    outbound = {
        "protocol": "shadowsocks",
        "settings": {
            "servers": [{
                "address": address,
                "port": port,
                "method": method,
                "password": password,
            }]
        },
        "streamSettings": {"network": "tcp"},
    }
    return ParsedConfig("shadowsocks", address, port, outbound, link, name)


_PARSERS = {
    "vmess://": _parse_vmess,
    "vless://": _parse_vless,
    "trojan://": _parse_trojan,
    "ss://": _parse_ss,
}


def parse_link(link: str) -> Optional[ParsedConfig]:
    link = link.strip()
    for prefix, parser in _PARSERS.items():
        if link.startswith(prefix):
            try:
                return parser(link)
            except Exception:
                return None
    return None


# --------------------------------------------------------------------------- #
# rebuild a link with a new remark/name                                        #
# --------------------------------------------------------------------------- #
def rebuild_link(link: str, new_name: str) -> str:
    link = link.strip()
    if link.startswith("vmess://"):
        try:
            info = json.loads(_b64decode_str(link[len("vmess://"):]))
            info["ps"] = new_name
            raw = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return "vmess://" + base64.b64encode(raw).decode("ascii")
        except Exception:
            return link
    # vless / trojan / ss -> replace the URL fragment
    base = link.split("#", 1)[0]
    return base + "#" + urllib.parse.quote(new_name)


def dedup_key(cfg: ParsedConfig) -> str:
    """Identity used to drop duplicate servers before testing."""
    return f"{cfg.protocol}|{cfg.address.lower()}|{cfg.port}"
