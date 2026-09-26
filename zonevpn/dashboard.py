"""Web console for the collector: status, live progress, logs, the server table.

Run it:   ./venv/bin/python -m zonevpn.dashboard
          (install.sh installs it as a systemd service)

What it shows / does:
  • Overall status   — last cycle time, published count, signed/base64 flags.
  • Live progress    — the cycle in flight (collected, reachable, tested, alive).
  • Live logs        — tails the collector's log.
  • Server table     — the *decoded* published list (name, ping, country, flag,
                       protocol, host:port) even though the gist is base64.
  • Delete           — drops a server: adds it to the blocklist (so it never
                       comes back) AND immediately re-publishes the gist without
                       it, so it's gone from the app right away.
  • Update           — runs update.sh (hard `git reset` + restart) via sudo.

## Sign-in

A username and password, set on the server with

    ./venv/bin/python -m zonevpn.dashboard set-login

which stores a scrypt hash in config.json (`dashboard_user`,
`dashboard_pass_hash`). Signing in gives an HttpOnly session cookie. It
replaced a token in the page URL, which the access log wrote down with every
request.

## What a stranger sees

This box is in Iran and its address is scanned. Before signing in there is a
plain "Sign in" page at / and a 404 for every other path — the API included —
with no product name, no mention of a VPN, and a generic Server header. The
page is served over HTTPS (a self-signed certificate for "localhost",
generated on first start into state/tls/) so neither the password nor the
server list crosses the network in the clear; turn it off with
`dashboard_tls: false` only behind an SSH tunnel.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from . import config as cfgmod
from . import gist, links, sign, state

log = logging.getLogger("zonevpn.dashboard")

ROOT = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = ROOT / "update.sh"
SERVICE_NAME = "zonevpn"              # the collector
CONSOLE_NAME = "zonevpn-dashboard"    # this console


# --------------------------------------------------------------------------- #
# helpers                                                                       #
# --------------------------------------------------------------------------- #
def _service(name: str) -> dict:
    """systemd's view of a unit: its state, and since when (epoch seconds)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", name, "--timestamp=unix",
             "-p", "ActiveState", "-p", "ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5).stdout
        props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        since = props.get("ActiveEnterTimestamp", "").lstrip("@")
        return {"state": props.get("ActiveState") or "unknown",
                "since": int(since) if since.isdigit() else None}
    except Exception:
        return {"state": "unknown", "since": None}


def _service_active() -> str:
    return _service(SERVICE_NAME)["state"]


def _system() -> dict:
    """Load, memory, disk and uptime — what a 2-core, 2 GB box runs out of."""
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError):
        load = None
    mem: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, value = line.split(":", 1)
                mem[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/uptime", encoding="ascii") as fh:
            uptime = float(fh.read().split()[0])
    except (OSError, ValueError):
        uptime = None
    disk = shutil.disk_usage(str(ROOT))
    return {
        "load": load, "cpus": os.cpu_count(),
        "mem_total": mem.get("MemTotal"), "mem_available": mem.get("MemAvailable"),
        "disk_total": disk.total, "disk_used": disk.used, "uptime_s": uptime,
    }


def _tail(path: Path, n: int) -> str:
    """The last [n] lines, read from the end rather than the whole file."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            chunk = min(size, max(64_000, n * 400))
            fh.seek(size - chunk)
            data = fh.read()
    except OSError:
        return "(no logs yet)"
    lines = data.decode("utf-8", errors="replace").splitlines()
    if chunk < size:
        lines = lines[1:]  # the first one is cut
    return "\n".join(lines[-n:])


# ── cached reads of what the collector writes ──────────────────────────────
_cache: dict[str, tuple] = {}


def _cached(path: Path, parse):
    """[parse] the file again only when it has changed."""
    try:
        st = path.stat()
    except OSError:
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    value = parse(path)
    _cache[str(path)] = (stamp, value)
    return value


def _reliability() -> dict:
    def parse(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return _cached(state.STATE_DIR / "reliability.json", parse) or {}


_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w+) ")
_COLLECTED = re.compile(r"collected (\d+) raw links -> (\d+) unique")
_SCREENED = re.compile(r"screening: (\d+) of (\d+) endpoints answered")
_ALIVE = re.compile(r"alive after testing: (\d+) / (\d+)")
_PUBLISHED = re.compile(r"published (\d+) configs to gist \S+ \(([\d.]+)s total\)")
_NOT_PUBLISHED = re.compile(r"not publishing|publish failed")
_YIELD = re.compile(r"source yield:\s+(\d+) alive /\s+(\d+) tested\s+(.+?)\s*$")


def _history() -> dict:
    """Past cycles and the latest per-source yield, read back out of the log.

    The collector already writes one line per step of every cycle; reading
    them back needs no change to the collector and covers every cycle the
    log (and its previous rotation) still holds.
    """
    def parse(_current: Path):
        lines: list[str] = []
        for path in (Path(str(state.LOG_FILE) + ".1"), state.LOG_FILE):
            try:
                lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                pass
        cycles, cur = [], {"warnings": 0, "errors": 0, "sources": []}
        for line in lines:
            m = _LINE.match(line)
            if not m:
                continue
            ts, level = m.group(1), m.group(2)
            if level == "WARNING":
                cur["warnings"] += 1
            elif level in ("ERROR", "CRITICAL"):
                cur["errors"] += 1
            if (hit := _COLLECTED.search(line)):
                cur.update(raw=int(hit.group(1)), collected=int(hit.group(2)),
                           started=ts)
            elif (hit := _SCREENED.search(line)):
                cur.update(answered=int(hit.group(1)), screened=int(hit.group(2)))
            elif (hit := _ALIVE.search(line)):
                cur.update(alive=int(hit.group(1)), tested=int(hit.group(2)))
            elif (hit := _YIELD.search(line)):
                cur["sources"].append({"name": hit.group(3),
                                       "alive": int(hit.group(1)),
                                       "tested": int(hit.group(2))})
            elif (hit := _PUBLISHED.search(line)):
                cur.update(published=int(hit.group(1)),
                           duration=float(hit.group(2)), ended=ts, ok=True)
                cycles.append(cur)
                cur = {"warnings": 0, "errors": 0, "sources": []}
            elif _NOT_PUBLISHED.search(line) and "collected" in cur:
                cur.update(ended=ts, ok=False)
                cycles.append(cur)
                cur = {"warnings": 0, "errors": 0, "sources": []}
        sources = next((c["sources"] for c in reversed(cycles) if c["sources"]), [])
        sources = sorted(sources, key=lambda s: (-s["alive"], -s["tested"]))
        for c in cycles:
            c.pop("sources", None)
        return {"cycles": cycles[-48:], "sources": sources}
    return _cached(state.LOG_FILE, parse) or {"cycles": [], "sources": []}


def _republish_without(deleted_key: str) -> tuple[bool, str]:
    """Rebuild the payload from the local snapshot minus the deleted server and
    push it to the gist now, so the app stops seeing it immediately."""
    cfg, cfg_err = cfgmod.load_lenient()
    if cfg_err:
        return False, f"config.json is broken: {cfg_err}"
    if not cfg.get("github_token") or not cfg.get("gist_id"):
        return False, "github_token/gist_id not configured"

    servers = [s for s in state.read_servers()
               if s.get("block_key") != deleted_key]
    # Strip dashboard-only fields so the public gist keeps its clean shape.
    _internal = {"block_key", "exit_ip", "front"}
    configs = [{k: v for k, v in s.items() if k not in _internal} for s in servers]
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(configs),
        "configs": configs,
        "raw": "\n".join(c.get("config", "") for c in configs),
    }
    sign_key = sign.load_private_key(cfg)
    ok = gist.publish(
        cfg["github_token"], cfg["gist_id"], cfg["gist_filename"], payload,
        base64_encode=bool(cfg.get("gist_base64", True)),
        sign_key_b64=sign_key,
    )
    if ok:
        # reflect the deletion in the local snapshot too
        state.write_servers(servers)
    return ok, ("re-published" if ok else "gist publish failed")


# --------------------------------------------------------------------------- #
# sign-in                                                                       #
# --------------------------------------------------------------------------- #
SESSION_COOKIE = "sid"
SESSION_TTL = 7 * 24 * 3600          # a week, then sign in again
_sessions: dict[str, float] = {}      # session id -> expiry (epoch seconds)

# Guessing: after this many wrong passwords from one address inside the
# window, that address is not even checked until the window has passed.
_FAIL_LIMIT = 6
_FAIL_WINDOW = 15 * 60
_failures: dict[str, list[float]] = {}

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        salt=base64.b64encode(salt).decode(),
        digest=base64.b64encode(digest).decode(), **_SCRYPT)


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _login_config() -> tuple[str, str]:
    """Read fresh on every attempt, so `set-login` needs no restart."""
    cfg, _err = cfgmod.load_lenient()
    return (str(cfg.get("dashboard_user") or ""),
            str(cfg.get("dashboard_pass_hash") or ""))


def _client_ip(request: web.Request) -> str:
    return request.remote or "?"


def _locked_out(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < _FAIL_WINDOW]
    _failures[ip] = recent
    return len(recent) >= _FAIL_LIMIT


def _signed_in(request: web.Request) -> bool:
    sid = request.cookies.get(SESSION_COOKIE, "")
    expiry = _sessions.get(sid)
    if expiry is None:
        return False
    if expiry < time.time():
        _sessions.pop(sid, None)
        return False
    return True


@web.middleware
async def _gate(request: web.Request, handler):
    if _signed_in(request):
        return await handler(request)
    # Signed out, the only things that exist are the sign-in page and the form
    # it posts to. Everything else — the API included — is a plain 404, so a
    # scanner learns nothing about what runs here.
    if request.path == "/" and request.method in ("GET", "HEAD"):
        return _login_page()
    if request.path == "/login" and request.method == "POST":
        return await handler(request)
    raise web.HTTPNotFound()


async def _headers(_request: web.Request, response: web.StreamResponse) -> None:
    # aiohttp names itself and Python by default. Nothing here should say what
    # this box is.
    response.headers["Server"] = "nginx"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers.setdefault("Cache-Control", "no-store")


def _login_page(error: str = "") -> web.Response:
    note = f'<p class="err">{error}</p>' if error else ""
    return web.Response(text=_LOGIN_HTML.replace("<!--ERROR-->", note),
                        content_type="text/html")


async def login(request: web.Request) -> web.Response:
    ip = _client_ip(request)
    if _locked_out(ip):
        await asyncio.sleep(1.0)
        return _login_page("Invalid username or password.")
    form = await request.post()
    user_in = str(form.get("username", "")).strip()
    pass_in = str(form.get("password", ""))
    user, stored = _login_config()
    # Bytes, not str: compare_digest refuses non-ASCII text, and a username
    # may well be written in Persian.
    ok = bool(user and stored) \
        and hmac.compare_digest(user_in.encode(), user.encode()) \
        and verify_password(pass_in, stored)
    if not ok:
        _failures.setdefault(ip, []).append(time.time())
        log.warning("sign-in failed from %s", ip)
        await asyncio.sleep(1.0)
        return _login_page("Invalid username or password.")
    _failures.pop(ip, None)
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = time.time() + SESSION_TTL
    log.info("signed in from %s", ip)
    response = web.Response(status=303, headers={"Location": "/"})
    response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True,
                        samesite="Strict", secure=request.secure, path="/")
    return response


async def logout(request: web.Request) -> web.Response:
    _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response = web.Response(status=303, headers={"Location": "/"})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


# --------------------------------------------------------------------------- #
# routes                                                                        #
# --------------------------------------------------------------------------- #
async def index(_request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html")


async def api_status(request: web.Request) -> web.Response:
    status = state.read_status()
    servers = state.read_servers()
    history = _reliability()
    for s in servers:
        # The last few cycles of this endpoint, oldest first: the table draws
        # them as dots, which says more than one percentage.
        s["history"] = history.get(s.get("front") or "") or \
            history.get(s.get("block_key") or "") or []
    age = None
    if status.get("updated_at"):
        try:
            stamp = datetime.strptime(status["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
            age = int((datetime.now(timezone.utc)
                       - stamp.replace(tzinfo=timezone.utc)).total_seconds())
        except ValueError:
            pass
    return web.json_response({
        "status": status,
        "list_age_s": age,
        "servers": servers,
        "blocklist": state.load_blocklist(),
        "manual": state.read_manual(),
        "progress": state.read_progress(),
        "service": _service_active(),
        "services": {"collector": _service(SERVICE_NAME),
                     "console": _service(CONSOLE_NAME)},
        "system": _system(),
        "config_error": request.app.get("config_error"),
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


async def api_history(_request: web.Request) -> web.Response:
    return web.json_response(_history())


async def api_add(request: web.Request) -> web.Response:
    """Add a server by share link. It's parsed/validated, stored, and picked up
    (tested + published if it's actually fast) on the next cycle."""
    body = await request.json()
    link = (body.get("link") or "").strip()
    if not link:
        return web.json_response({"error": "link required"}, status=400)
    parsed = links.parse_link(link)
    if parsed is None:
        return web.json_response(
            {"ok": False,
             "message": "unrecognized link (need vmess/vless/trojan/ss://)"},
            status=400)
    manual = state.add_manual(link)
    return web.json_response({
        "ok": True,
        "message": f"added {parsed.protocol} {parsed.address}:{parsed.port} — "
                   f"it will be tested on the next cycle",
        "count": len(manual),
    })


async def api_remove_manual(request: web.Request) -> web.Response:
    body = await request.json()
    link = (body.get("link") or "").strip()
    if not link:
        return web.json_response({"error": "link required"}, status=400)
    state.remove_manual(link)
    return web.json_response({"ok": True})


_LOG_CHUNK = 512_000


async def api_logs(request: web.Request) -> web.Response:
    """The log, a little at a time.

    With `since` (the offset the last answer ended at), only what was written
    after it — a few lines every few seconds instead of the last 1,500 lines
    again and again. Without it, or after the log has rotated underneath it,
    the last `n` lines and a fresh offset.
    """
    path = state.LOG_FILE
    try:
        size = path.stat().st_size
    except OSError:
        return web.json_response({"text": "", "offset": 0, "reset": True})
    since = request.query.get("since", "")
    if since.isdigit() and int(since) <= size:
        start = max(int(since), size - _LOG_CHUNK)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
        return web.json_response({
            "text": data.decode("utf-8", errors="replace"),
            "offset": size, "reset": False})
    n = int(request.query.get("n", "400") or "400")
    return web.json_response({"text": _tail(path, max(1, min(n, 3000))),
                              "offset": size, "reset": True})


async def api_logs_download(_request: web.Request) -> web.StreamResponse:
    if not state.LOG_FILE.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(state.LOG_FILE, headers={
        "Content-Disposition": 'attachment; filename="console.log"'})


async def api_delete(request: web.Request) -> web.Response:
    body = await request.json()
    key = (body.get("block_key") or "").strip()
    if not key:
        return web.json_response({"error": "block_key required"}, status=400)
    state.add_to_blocklist(key)
    ok, msg = _republish_without(key)
    return web.json_response({"ok": ok, "message": msg, "block_key": key})


async def api_restore(request: web.Request) -> web.Response:
    body = await request.json()
    key = (body.get("block_key") or "").strip()
    if not key:
        return web.json_response({"error": "block_key required"}, status=400)
    state.remove_from_blocklist(key)
    return web.json_response({"ok": True, "block_key": key})


async def api_restart(_request: web.Request) -> web.Response:
    """Restarts the collector, which starts a fresh cycle straight away."""
    try:
        subprocess.Popen(["systemctl", "restart", SERVICE_NAME],
                         start_new_session=True)
        return web.json_response({"ok": True,
                                  "message": "collector restarting; a new cycle starts now"})
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=500)


async def api_update(_request: web.Request) -> web.Response:
    if not UPDATE_SCRIPT.exists():
        return web.json_response({"ok": False, "message": "update.sh missing"},
                                 status=500)
    try:
        # Detached so it survives this (dashboard) service being restarted.
        subprocess.Popen(["sudo", "-n", str(UPDATE_SCRIPT)],
                         cwd=str(ROOT), start_new_session=True)
        return web.json_response({"ok": True,
                                  "message": "update started; services restarting…"})
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=500)


def build_app(config_error: str | None = None) -> web.Application:
    app = web.Application(middlewares=[_gate])
    app["config_error"] = config_error
    app.on_response_prepare.append(_headers)
    app.add_routes([
        web.get("/", index),
        web.post("/login", login),
        web.post("/logout", logout),
        web.get("/api/status", api_status),
        web.get("/api/history", api_history),
        web.get("/api/logs", api_logs),
        web.get("/api/logs/download", api_logs_download),
        web.post("/api/restart", api_restart),
        web.post("/api/delete", api_delete),
        web.post("/api/restore", api_restore),
        web.post("/api/add", api_add),
        web.post("/api/remove_manual", api_remove_manual),
        web.post("/api/update", api_update),
    ])
    return app


# --------------------------------------------------------------------------- #
# HTTPS                                                                         #
# --------------------------------------------------------------------------- #
TLS_DIR = state.STATE_DIR / "tls"


def _tls_context() -> ssl.SSLContext | None:
    """A self-signed certificate for "localhost", made once and kept.

    Deliberately generic: a certificate is the first thing a TLS scanner
    records, and a name on it would say what this box is.
    """
    cert, key = TLS_DIR / "cert.pem", TLS_DIR / "key.pem"
    try:
        if not (cert.exists() and key.exists()):
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.x509.oid import NameOID

            TLS_DIR.mkdir(parents=True, exist_ok=True)
            private = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.now(timezone.utc)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(private.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.DNSName("localhost")]), critical=False)
                .sign(private, hashes.SHA256()))
            old = os.umask(0o077)
            try:
                key.write_bytes(private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()))
                cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            finally:
                os.umask(old)
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(str(cert), str(key))
        return context
    except Exception as exc:  # the console must still come up
        log.error("HTTPS unavailable (%s); serving plain HTTP", exc)
        return None


# --------------------------------------------------------------------------- #
# set-login                                                                     #
# --------------------------------------------------------------------------- #
def set_login() -> None:
    """Asks for a username and password on this terminal and stores the hash.

    The password is typed here and nowhere else; only its scrypt hash is
    written to config.json.
    """
    user = input("Username: ").strip()
    if not user:
        sys.exit("A username is required.")
    password = getpass.getpass("Password (at least 10 characters): ")
    if len(password) < 10:
        sys.exit("Too short: use at least 10 characters.")
    if getpass.getpass("Password again: ") != password:
        sys.exit("The two passwords differ; nothing was changed.")

    path = cfgmod.CONFIG_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data["dashboard_user"] = user
    data["dashboard_pass_hash"] = hash_password(password)
    data.pop("dashboard_token", None)  # the old URL token no longer opens anything
    tmp = path.with_suffix(".json.tmp")
    old = os.umask(0o077)
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    finally:
        os.umask(old)
    print("Saved. It takes effect at the next sign-in; no restart needed.")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "set-login":
        set_login()
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Lenient load: even if config.json is broken, still bring the console up so
    # the operator can see the error + logs and fix it.
    cfg, cfg_err = cfgmod.load_lenient()
    host = cfg.get("dashboard_host", "0.0.0.0")
    port = int(cfg.get("dashboard_port", 8787))
    state.ensure_dir()
    if cfg_err:
        log.error("config.json problem: %s — running in LIMITED mode "
                  "(publish/delete disabled until fixed)", cfg_err)
    if not (cfg.get("dashboard_user") and cfg.get("dashboard_pass_hash")):
        log.warning("no sign-in is set, so nobody can sign in. Run: "
                    "./venv/bin/python -m zonevpn.dashboard set-login")
    context = _tls_context() if cfg.get("dashboard_tls", True) else None
    log.info("console on %s://%s:%d", "https" if context else "http", host, port)
    web.run_app(build_app(cfg_err), host=host, port=port, ssl_context=context,
                print=None)


_LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex, nofollow"/>
<title>Sign in</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;
    color:#e7e9ee;font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  form{width:min(340px,calc(100vw - 32px));background:#171a21;border:1px solid #262a33;
    border-radius:16px;padding:26px 24px 22px;box-shadow:0 20px 50px rgba(0,0,0,.35)}
  h1{margin:0 0 18px;font-size:19px;font-weight:650}
  label{display:block;font-size:12.5px;color:#9aa1ad;margin:0 0 6px}
  input{width:100%;padding:11px 12px;margin:0 0 14px;background:#0f1115;color:#e7e9ee;
    border:1px solid #2c313b;border-radius:10px;font-size:15px}
  input:focus{outline:none;border-color:#5b8cff}
  button{width:100%;padding:11px;border:0;border-radius:10px;background:#5b8cff;color:#fff;
    font-weight:600;font-size:15px;cursor:pointer}
  .err{margin:0 0 14px;color:#ff8a93;font-size:13px}
</style>
</head>
<body>
<form method="post" action="/login" autocomplete="on">
  <h1>Sign in</h1>
  <!--ERROR-->
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" required autofocus/>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required/>
  <button type="submit">Sign in</button>
</form>
</body>
</html>
"""


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex, nofollow"/>
<title>Console</title>
<style>
:root{
  --bg:#0a0c11;--bg2:#0d1017;--card:#11141b;--card2:#151923;--line:#1d2230;--line2:#272d3c;
  --tx:#e8ebf2;--tx2:#a3acbd;--tx3:#6b7588;
  --acc:#6c8cff;--acc2:#8b6cff;--ok:#2fd39c;--warn:#f3b44b;--bad:#ff6b7a;--r:16px;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--tx);min-height:100vh;
  font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  background-image:radial-gradient(900px 480px at 88% -140px,rgba(108,140,255,.16),transparent 60%),
    radial-gradient(700px 420px at -12% 8%,rgba(139,108,255,.09),transparent 60%);
  background-attachment:fixed}
a{color:inherit;text-decoration:none}
.wrap{max-width:1260px;margin:0 auto;padding:18px 18px 64px}
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px}
.brand i{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--acc),var(--acc2));
  box-shadow:0 6px 18px rgba(108,140,255,.35)}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;background:var(--card);
  border:1px solid var(--line);color:var(--tx2);font-size:12.5px;white-space:nowrap}
.pill b{color:var(--tx);font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:var(--tx3);flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.dot.live{animation:pulse 1.8s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,211,156,.55)}70%{box-shadow:0 0 0 7px rgba(47,211,156,0)}100%{box-shadow:0 0 0 0 rgba(47,211,156,0)}}
.actions{margin-left:auto;display:flex;gap:8px;align-items:center}
.btn{appearance:none;border:1px solid var(--line2);background:var(--card);color:var(--tx);padding:8px 13px;
  border-radius:10px;font:600 13px/1.2 inherit;cursor:pointer;display:inline-flex;align-items:center;gap:7px;
  transition:border-color .15s,background .15s}
.btn:hover{border-color:#36415c;background:var(--card2)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:linear-gradient(135deg,var(--acc),var(--acc2));border-color:transparent;color:#fff}
.btn.danger{color:var(--bad);border-color:#3b2330}.btn.danger:hover{background:#1d1318}
.btn.ghost{background:transparent}
.btn.sm{padding:6px 9px;font-size:12px;border-radius:8px}
.btn:disabled{opacity:.5;cursor:default}
.banner{display:none;gap:10px;padding:12px 14px;border-radius:12px;margin-bottom:14px;font-size:13.5px;border:1px solid}
.banner.show{display:flex}
.banner.bad{background:#1c1015;border-color:#4a2330;color:#ffc2c9}
.banner.warn{background:#1c1710;border-color:#4a3a1f;color:#ffe0a8}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px}
.card{background:linear-gradient(180deg,var(--card),#0f1218);border:1px solid var(--line);border-radius:var(--r);min-width:0}
.card>h2{margin:0;padding:14px 16px 0;font-size:12.5px;font-weight:650;color:var(--tx2);text-transform:uppercase;
  letter-spacing:.7px;display:flex;align-items:center;gap:10px}
.card>h2 .r{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:500;color:var(--tx3);font-size:12.5px}
.pad{padding:14px 16px 16px}
.s12{grid-column:span 12}.s8{grid-column:span 8}.s4{grid-column:span 4}.s7{grid-column:span 7}.s5{grid-column:span 5}
@media(max-width:1000px){.s8,.s4,.s7,.s5{grid-column:span 12}}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr))}
.kpi{padding:14px 16px;border-right:1px solid var(--line)}.kpi:last-child{border-right:0}
.kpi .k{color:var(--tx3);font-size:11.5px;text-transform:uppercase;letter-spacing:.6px}
.kpi .v{font-size:24px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums;white-space:nowrap}
.kpi .d{font-size:12px;color:var(--tx3);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.up{color:var(--ok)}.down{color:var(--bad)}
@media(max-width:1000px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi{border-bottom:1px solid var(--line)}
  .kpi:nth-child(2n){border-right:0}.kpi:last-child{grid-column:span 2}}
.steps{display:flex;gap:6px;margin:2px 0 14px}
.step{flex:1;text-align:center;font-size:12px;color:var(--tx3);padding-top:10px;position:relative;white-space:nowrap}
.step:before{content:"";position:absolute;top:0;left:0;right:0;height:4px;border-radius:4px;background:var(--line2)}
.step.done{color:var(--tx2)}.step.done:before{background:linear-gradient(90deg,var(--acc),var(--acc2))}
.step.now{color:var(--tx);font-weight:600}
.step.now:before{background:linear-gradient(90deg,var(--ok),#7ef0cb);animation:glow 1.2s ease-in-out infinite alternate}
@keyframes glow{from{opacity:.5}to{opacity:1}}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
@media(max-width:560px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
.stat{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.stat .k{color:var(--tx3);font-size:11.5px}.stat .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--line2);border-radius:6px;overflow:hidden;margin:14px 0 10px}
.bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--ok));border-radius:6px;transition:width .5s}
.feed{margin:0;height:132px;overflow:auto;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color:#b9c6dd;background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:10px 12px;white-space:pre}
.meter{margin-bottom:13px}
.meter .row{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;color:var(--tx2);margin-bottom:6px}
.meter .row b{color:var(--tx);font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.meter .track{height:8px;background:var(--line2);border-radius:8px;overflow:hidden}
.meter .track i{display:block;height:100%;border-radius:8px;transition:width .5s}
.kv{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;font-size:12.5px;color:var(--tx2);margin-top:4px}
.kv b{color:var(--tx);font-weight:600;text-align:right}
.chart{width:100%;height:200px;display:block}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--tx3);padding:0 16px 14px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.tip{position:fixed;pointer-events:none;background:#0b0e14;border:1px solid var(--line2);border-radius:10px;
  padding:8px 10px;font-size:12px;color:var(--tx2);opacity:0;transition:opacity .1s;z-index:30;white-space:nowrap;line-height:1.6}
.tip b{color:var(--tx)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:12px 16px}
.input{background:var(--bg2);border:1px solid var(--line2);color:var(--tx);padding:9px 12px;border-radius:10px;
  font:13px inherit;min-width:0}
.input:focus{outline:none;border-color:var(--acc)}
.grow{flex:1 1 220px}
.chips{display:flex;gap:6px;flex-wrap:wrap;padding:0 16px 12px}
.chip{border:1px solid var(--line2);background:var(--bg2);color:var(--tx2);padding:4px 10px;border-radius:999px;
  font-size:12px;cursor:pointer;user-select:none}
.chip b{color:var(--tx);margin-left:5px;font-weight:600}
.chip.on{border-color:var(--acc);color:#fff;background:rgba(108,140,255,.16)}
.tbl{overflow:auto;max-height:560px;border-top:1px solid var(--line)}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #161a24;font-size:13px;white-space:nowrap}
th{position:sticky;top:0;background:#12151d;color:var(--tx3);font-size:11px;text-transform:uppercase;letter-spacing:.5px;
  font-weight:600;z-index:1;cursor:pointer;user-select:none}
th.nosort{cursor:default}
th .arr{margin-left:4px;color:var(--acc)}
tbody tr:hover td{background:#131826}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--tx2);font-size:12.5px}
.muted{color:var(--tx3)}.good{color:var(--ok)}.mid{color:var(--warn)}.bad{color:var(--bad)}
.hist{display:inline-flex;gap:3px;vertical-align:middle;margin-right:7px}
.hist i{width:7px;height:7px;border-radius:2px;background:var(--line2)}
.hist i.p{background:var(--ok)}.hist i.f{background:var(--bad)}
.rowact{display:flex;gap:6px;justify-content:flex-end}
.ip{max-width:210px;overflow:hidden;text-overflow:ellipsis}
.no-tcp th:nth-child(6),.no-tcp td:nth-child(6),.no-kbps th:nth-child(7),.no-kbps td:nth-child(7){display:none}
#blocked{max-height:230px;overflow:auto}
.empty{padding:26px!important;text-align:center!important;color:var(--tx3);white-space:normal!important}
.sub{padding:12px 16px 14px;border-top:1px solid var(--line)}
.sub h3{margin:0 0 9px;font-size:11.5px;color:var(--tx3);text-transform:uppercase;letter-spacing:.6px;font-weight:600}
.item{display:flex;gap:10px;align-items:center;padding:7px 0;border-top:1px solid #161a24}
.item:first-child{border-top:0}
.item span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ybar{height:6px;border-radius:6px;background:var(--line2);overflow:hidden;min-width:60px}
.ybar i{display:block;height:100%;background:var(--ok);border-radius:6px}
.logbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 16px}
.seg{display:inline-flex;border:1px solid var(--line2);border-radius:10px;overflow:hidden}
.seg button{border:0;background:var(--bg2);color:var(--tx2);padding:7px 11px;font:600 12px inherit;cursor:pointer}
.seg button.on{background:rgba(108,140,255,.2);color:#fff}
#log{margin:0;height:440px;overflow:auto;padding:12px 16px;border-top:1px solid var(--line);background:#0b0e14;
  font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#b7c3d7;white-space:pre-wrap;
  word-break:break-word;border-radius:0 0 var(--r) var(--r)}
#log .w{color:var(--warn)}#log .e{color:var(--bad)}#log .t{color:#566079}
.modal{position:fixed;inset:0;background:rgba(5,7,11,.66);display:none;align-items:center;justify-content:center;z-index:50}
.modal.show{display:flex}
.dialog{width:min(430px,calc(100vw - 32px));background:var(--card);border:1px solid var(--line2);border-radius:16px;
  padding:20px;box-shadow:0 30px 80px rgba(0,0,0,.5)}
.dialog h4{margin:0 0 8px;font-size:16px}.dialog p{margin:0 0 16px;color:var(--tx2);font-size:13.5px;word-break:break-word}
.dialog .btns{display:flex;gap:8px;justify-content:flex-end}
.toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,16px);background:#121827;border:1px solid var(--line2);
  padding:11px 16px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none;z-index:60;font-size:13px;
  box-shadow:0 12px 40px rgba(0,0,0,.4);max-width:calc(100vw - 32px)}
.toast.show{opacity:1;transform:translate(-50%,0)}
</style>
</head>
<body>
<div class="wrap">
 <header class="top">
  <div class="brand"><i></i>Console</div>
  <div class="pills">
   <span class="pill" id="pList"><span class="dot"></span>List <b>–</b></span>
   <span class="pill" id="pCollector"><span class="dot"></span>Collector <b>–</b></span>
   <span class="pill" id="pConn"><span class="dot"></span><b>Connecting…</b></span>
  </div>
  <div class="actions">
   <button class="btn" id="bRestart" title="Restart the collector — a new cycle starts straight away">⟳ New cycle</button>
   <button class="btn primary" id="bUpdate" title="Pull the latest code and restart both services">⬆ Update</button>
   <form method="post" action="/logout" style="margin:0"><button class="btn ghost" title="Sign out">Sign out</button></form>
  </div>
 </header>
 <div class="banner" id="banner"></div>
 <main class="grid">
  <section class="card s12"><div class="kpis">
   <div class="kpi"><div class="k">Published</div><div class="v" id="kPub">–</div><div class="d" id="kPubD">&nbsp;</div></div>
   <div class="kpi"><div class="k">List age</div><div class="v" id="kAge">–</div><div class="d" id="kAgeD">&nbsp;</div></div>
   <div class="kpi"><div class="k">Last cycle</div><div class="v" id="kDur">–</div><div class="d" id="kDurD">&nbsp;</div></div>
   <div class="kpi"><div class="k">Alive / tested</div><div class="v" id="kAlive">–</div><div class="d" id="kAliveD">&nbsp;</div></div>
   <div class="kpi"><div class="k">Countries</div><div class="v" id="kCc">–</div><div class="d" id="kCcD">&nbsp;</div></div>
  </div></section>

  <section class="card s8"><h2>Current cycle <span class="r" id="cyWhen"></span></h2><div class="pad">
   <div class="steps" id="steps"></div>
   <div class="stats">
    <div class="stat"><div class="k">Collected</div><div class="v" id="cyCol">–</div></div>
    <div class="stat"><div class="k">Reachable</div><div class="v" id="cyReach">–</div></div>
    <div class="stat"><div class="k">Tested</div><div class="v" id="cyTest">–</div></div>
    <div class="stat"><div class="k">Alive</div><div class="v" id="cyAlive">–</div></div>
   </div>
   <div class="bar"><i id="cyBar"></i></div>
   <pre class="feed" id="feed">…</pre>
  </div></section>

  <section class="card s4"><h2>System <span class="r" id="sysUp"></span></h2><div class="pad" id="sys"></div></section>

  <section class="card s12"><h2>Cycles <span class="r" id="histNote"></span></h2>
   <svg class="chart" id="chart"></svg>
   <div class="legend"><span><i style="background:linear-gradient(180deg,#6c8cff,#8b6cff)"></i>published</span>
    <span><i style="background:rgba(47,211,156,.45)"></i>alive after testing</span>
    <span><i style="background:#f3b44b;height:3px;vertical-align:3px"></i>cycle time</span>
    <span><i style="background:#ff6b7a"></i>not published</span></div>
  </section>

  <section class="card s12"><h2>Servers <span class="r" id="sCount"></span></h2>
   <div class="toolbar">
    <input class="input grow" id="q" placeholder="Search name, host, IP, country…   ( / )"/>
    <select class="input" id="fProto"><option value="">All protocols</option></select>
    <button class="btn sm" id="bClear">Clear filters</button>
   </div>
   <div class="chips" id="ccChips"></div>
   <div class="tbl"><table id="stbl">
    <thead><tr>
     <th class="nosort">#</th><th data-k="country">Server</th><th data-k="front">Front</th><th data-k="exit_ip">Exit IP</th>
     <th data-k="ping">Ping</th><th data-k="tcp_ping">TCP</th><th data-k="kbps">Speed</th>
     <th data-k="reliability">Reliability</th><th data-k="protocol">Proto</th><th class="nosort"></th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="10" class="empty">loading…</td></tr></tbody>
   </table></div>
   <div class="sub">
    <h3>Add a server</h3>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><input class="input grow mono" id="addLink" placeholder="vmess:// vless:// trojan:// ss:// link"/>
     <button class="btn primary" id="bAdd">Add</button></div>
    <div id="manual" style="margin-top:8px"></div>
   </div>
   <div class="sub" id="blockedBox"><h3>Blocked <span id="bCount"></span></h3><div id="blocked"></div></div>
  </section>

  <section class="card s5"><h2>Sources <span class="r">yield in the last cycle</span></h2>
   <div class="tbl" style="max-height:470px;margin-top:12px"><table>
    <thead><tr><th class="nosort">Source</th><th class="nosort">Alive</th><th class="nosort">Tested</th><th class="nosort"></th></tr></thead>
    <tbody id="src"><tr><td colspan="4" class="empty">loading…</td></tr></tbody>
   </table></div>
  </section>

  <section class="card s7"><h2>Logs <span class="r"><a class="btn sm" href="/api/logs/download">Download</a></span></h2>
   <div class="logbar">
    <div class="seg" id="lvl"><button data-l="all" class="on">All</button><button data-l="warn">Warnings</button><button data-l="err">Errors</button></div>
    <input class="input grow" id="lq" placeholder="Filter lines…"/>
    <label class="muted" style="font-size:12.5px;display:flex;gap:6px;align-items:center;cursor:pointer"><input type="checkbox" id="follow" checked/>Follow</label>
   </div>
   <pre id="log">loading…</pre>
  </section>
 </main>
</div>
<div class="modal" id="modal"><div class="dialog"><h4 id="mT"></h4><p id="mB"></p>
 <div class="btns"><button class="btn" id="mNo">Cancel</button><button class="btn primary" id="mYes">OK</button></div></div></div>
<div class="toast" id="toast"></div>
<div class="tip" id="tip"></div>
<script>
(() => {
'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = n => n == null ? '–' : Number(n).toLocaleString('en-US');
function dur(s){
  if (s == null) return '–';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
  const h = Math.floor(m / 60);
  if (h < 48) return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
}
function bytes(b){
  if (b == null) return '–';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1){ b /= 1024; i++; }
  return b.toFixed(b < 10 && i ? 1 : 0) + ' ' + u[i];
}
function toast(m){
  const t = $('toast'); t.textContent = m; t.classList.add('show');
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2800);
}
function ask(title, body, yes = 'OK', danger = false){
  return new Promise(done => {
    const m = $('modal'), y = $('mYes'), n = $('mNo');
    $('mT').textContent = title; $('mB').textContent = body;
    y.textContent = yes; y.className = 'btn ' + (danger ? 'danger' : 'primary');
    const close = v => { m.classList.remove('show'); y.onclick = n.onclick = m.onclick = null; done(v); };
    y.onclick = () => close(true); n.onclick = () => close(false);
    m.onclick = e => { if (e.target === m) close(false); };
    m.classList.add('show'); y.focus();
  });
}
async function copyText(t){
  try { await navigator.clipboard.writeText(t); return true; } catch (_) {}
  const a = document.createElement('textarea');
  a.value = t; a.style.position = 'fixed'; a.style.opacity = '0'; document.body.appendChild(a); a.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch (_) {}
  a.remove(); return ok;
}

// ── talking to the server ─────────────────────────────────────────────────
function setConn(ok){
  $('pConn').innerHTML = ok ? '<span class="dot ok live"></span><b>Live</b>'
                            : '<span class="dot bad"></span><b>Offline</b>';
}
async function api(path, opts = {}){
  const r = await fetch(path, {credentials: 'same-origin', ...opts});
  // Signed out (a restart, a sign-out elsewhere): everything answers 404.
  if (r.status === 404 || r.status === 401){ location.replace('/'); throw new Error('signed out'); }
  return r;
}
async function post(path, body){
  const r = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  return r.json();
}

// ── UI state that survives a reload ───────────────────────────────────────
const UI = Object.assign({sort: 'ping', dir: 1, cc: '', proto: ''},
  (() => { try { return JSON.parse(localStorage.getItem('console.ui') || '{}'); } catch (_) { return {}; } })());
UI.q = '';
function saveUI(){ try { localStorage.setItem('console.ui', JSON.stringify({sort: UI.sort, dir: UI.dir, cc: UI.cc, proto: UI.proto})); } catch (_) {} }

let S = null, H = null, fetchedAt = 0, rowSig = '';

// ── status ────────────────────────────────────────────────────────────────
const PHASES = [['collecting','Collect'],['prefilter','Filter'],['sifting','Sift'],['screen','Screen'],['measure','Measure'],['publishing','Publish']];
function stepIndex(pr){
  if (!pr || !pr.active) return -1;
  let p = pr.phase;
  if (p === 'testing') p = pr.stage === 'measure' ? 'measure' : 'screen';
  return PHASES.findIndex(x => x[0] === p);
}
function listAge(){ return S && S.list_age_s != null ? S.list_age_s + (Date.now() - fetchedAt) / 1000 : null; }

function renderTop(){
  const age = listAge();
  const stale = age != null && age > 1200, dead = age != null && age > 3600;
  $('pList').innerHTML = `<span class="dot ${age == null ? '' : dead ? 'bad' : stale ? 'warn' : 'ok'}"></span>List <b>${age == null ? '–' : dur(age) + ' old'}</b>`;
  const cs = ((S.services || {}).collector || {}).state || S.service || 'unknown';
  $('pCollector').innerHTML = `<span class="dot ${cs === 'active' ? 'ok live' : cs === 'activating' || cs === 'reloading' ? 'warn' : 'bad'}"></span>Collector <b>${esc(cs)}</b>`;
  let msg = '', cls = '';
  if (S.config_error){ msg = 'config.json problem: ' + S.config_error + ' — publishing is off until it is fixed.'; cls = 'bad'; }
  else if (cs !== 'active'){ msg = 'The collector is ' + cs + ': nothing is being tested or published.'; cls = 'bad'; }
  else if (dead){ msg = 'The published list is ' + dur(age) + ' old. The collector is running but has not published since — check the logs below.'; cls = 'bad'; }
  else if (stale){ msg = 'The list has not been refreshed for ' + dur(age) + '.'; cls = 'warn'; }
  const b = $('banner'); b.className = 'banner' + (msg ? ' show ' + cls : ''); b.textContent = msg;
  $('kAge').textContent = age == null ? '–' : dur(age);
}

function renderKpis(){
  const st = S.status || {}, servers = S.servers || [];
  const cyc = (H && H.cycles) || [], last = cyc[cyc.length - 1], prev = cyc[cyc.length - 2];
  $('kPub').textContent = num(st.count ?? servers.length);
  if (last && prev && last.published != null && prev.published != null){
    const d = last.published - prev.published;
    $('kPubD').innerHTML = d === 0 ? 'same as the cycle before' : `<span class="${d > 0 ? 'up' : 'down'}">${d > 0 ? '▲' : '▼'} ${Math.abs(d)}</span> vs the cycle before`;
  } else $('kPubD').textContent = st.signed ? 'signed' : '';
  $('kAgeD').textContent = st.updated_at ? new Date(st.updated_at).toLocaleString() : '';
  $('kDur').textContent = st.duration_s != null ? dur(st.duration_s) : '–';
  $('kDurD').textContent = [st.signed ? 'signed' : 'unsigned', st.base64 ? 'base64' : 'plain', st.published_ok === false ? 'publish FAILED' : ''].filter(Boolean).join(' · ');
  if (last && last.tested){
    $('kAlive').textContent = num(last.alive) + ' / ' + num(last.tested);
    $('kAliveD').textContent = (last.alive / last.tested * 100).toFixed(2) + '% · ' + num(last.collected) + ' collected';
  } else { $('kAlive').textContent = '–'; $('kAliveD').innerHTML = '&nbsp;'; }
  const cc = {}; servers.forEach(s => { const c = s.country || '??'; cc[c] = (cc[c] || 0) + 1; });
  $('kCc').textContent = Object.keys(cc).length || '–';
  $('kCcD').textContent = Object.entries(cc).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([c, n]) => c + ' ' + n).join(' · ');
}

function renderCycle(){
  const pr = S.progress || {}, idx = stepIndex(pr);
  // Between cycles the live counters are empty; the last finished cycle is
  // the one worth reading then.
  const last = !pr.active && H && H.cycles && H.cycles.length ? H.cycles[H.cycles.length - 1] : null;
  const allDone = !pr.active && last && last.ok !== false;
  $('steps').innerHTML = PHASES.map(([k, label], i) =>
    `<div class="step ${allDone ? 'done' : idx < 0 ? '' : i < idx ? 'done' : i === idx ? 'now' : ''}">${label}</div>`).join('');
  if (last){
    $('cyCol').textContent = num(last.collected);
    $('cyReach').textContent = num(last.answered);
    $('cyTest').textContent = num(last.tested);
    $('cyAlive').textContent = num(last.alive);
    $('cyBar').style.width = '100%';
    $('cyWhen').textContent = 'idle — the last cycle ended ' + (last.ended || '').slice(11, 16) + ' UTC and published ' + num(last.published);
    $('feed').textContent = 'no cycle running — the next one starts on its own';
    return;
  }
  if (pr.active) $('cyWhen').textContent = '▶ ' + (pr.phase || '') + (pr.phase === 'testing' && pr.stage ? ' · ' + pr.stage : '');
  else $('cyWhen').textContent = pr.published != null ? 'idle — the last cycle published ' + pr.published : 'idle — waiting for the next cycle';
  $('cyCol').textContent = num(pr.collected);
  $('cyReach').textContent = num(pr.reachable);
  $('cyTest').textContent = (pr.tested != null ? num(pr.tested) : '–') + (pr.total ? ' / ' + num(pr.total) : '');
  $('cyAlive').textContent = num(pr.alive);
  const pct = pr.total ? Math.min(100, Math.round((pr.tested || 0) / pr.total * 100)) : (pr.active ? 4 : 0);
  $('cyBar').style.width = pct + '%';
  const feed = (pr.recent || []).slice().reverse()
    .map(r => `${String(r.ping).padStart(5)} ms   ${r.address}:${r.port}   ${r.protocol}`).join('\n');
  $('feed').textContent = feed || (pr.active ? 'waiting for the first results…' : 'no cycle running');
}

function meter(label, used, total, text, warnAt, badAt){
  const f = total ? used / total : 0;
  const c = f >= badAt ? 'var(--bad)' : f >= warnAt ? 'var(--warn)' : 'var(--ok)';
  return `<div class="meter"><div class="row"><span>${label}</span><b>${text}</b></div>` +
         `<div class="track"><i style="width:${Math.min(100, f * 100).toFixed(1)}%;background:${c}"></i></div></div>`;
}
function renderSystem(){
  const sys = S.system || {}, sv = S.services || {}, cpus = sys.cpus || 1, l = sys.load || [0, 0, 0];
  const memUsed = sys.mem_total && sys.mem_available != null ? sys.mem_total - sys.mem_available : null;
  const now = Date.now() / 1000, up = s => s ? dur(now - s) : '–';
  $('sys').innerHTML =
    meter('CPU load', l[0], cpus, l.map(x => x.toFixed(2)).join(' · ') + ' / ' + cpus + ' cores', .7, 1.0) +
    meter('Memory', memUsed, sys.mem_total, bytes(memUsed) + ' / ' + bytes(sys.mem_total), .8, .92) +
    meter('Disk', sys.disk_used, sys.disk_total, bytes(sys.disk_used) + ' / ' + bytes(sys.disk_total), .8, .92) +
    `<div class="kv"><span>Collector running for</span><b>${up((sv.collector || {}).since)}</b>` +
    `<span>Console running for</span><b>${up((sv.console || {}).since)}</b></div>`;
  $('sysUp').textContent = sys.uptime_s ? 'up ' + dur(sys.uptime_s) : '';
}

// ── servers ───────────────────────────────────────────────────────────────
const pingCls = p => p == null || p < 0 ? 'bad' : p <= 300 ? 'good' : p <= 900 ? 'mid' : 'bad';
const relCls = r => r == null ? 'muted' : r >= .85 ? 'good' : r >= .5 ? 'mid' : 'bad';
const speed = k => !k ? '–' : k >= 1000 ? (k / 1000).toFixed(1) + ' Mbps' : Math.round(k) + ' kbps';

function renderServers(){
  const servers = S.servers || [];
  const protos = [...new Set(servers.map(s => s.protocol).filter(Boolean))].sort();
  const opts = '<option value="">All protocols</option>' + protos.map(p => `<option${p === UI.proto ? ' selected' : ''}>${esc(p)}</option>`).join('');
  if ($('fProto').innerHTML !== opts) $('fProto').innerHTML = opts;

  const cc = {};
  servers.forEach(s => { const c = s.country || '??'; (cc[c] = cc[c] || {n: 0, flag: s.flag || ''}).n++; });
  // Country codes, not flag emoji: Windows draws a flag as its two letters,
  // and every row read "DE DE".
  $('ccChips').innerHTML = Object.entries(cc).sort((a, b) => b[1].n - a[1].n)
    .map(([c, v]) => `<span class="chip${UI.cc === c ? ' on' : ''}" data-cc="${esc(c)}">${esc(c)}<b>${v.n}</b></span>`).join('');
  // Columns nobody has a value for are dropped rather than filled with dashes.
  $('stbl').classList.toggle('no-tcp', !servers.some(s => s.tcp_ping > 0));
  $('stbl').classList.toggle('no-kbps', !servers.some(s => s.kbps > 0));

  const q = UI.q.toLowerCase();
  let list = servers.map((s, i) => Object.assign({_i: i}, s));
  if (UI.cc) list = list.filter(s => (s.country || '??') === UI.cc);
  if (UI.proto) list = list.filter(s => s.protocol === UI.proto);
  if (q) list = list.filter(s => [s.name, s.front, s.block_key, s.exit_ip, s.country, s.protocol].join(' ').toLowerCase().includes(q));
  const k = UI.sort, dir = UI.dir;
  const val = s => { const v = s[k]; if ((k === 'ping' || k === 'tcp_ping') && (v == null || v < 0)) return Infinity; return v ?? (typeof s.ping === 'number' ? -Infinity : ''); };
  list.sort((a, b) => { const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });

  document.querySelectorAll('th[data-k]').forEach(th => {
    th.dataset.label = th.dataset.label || th.textContent;
    th.innerHTML = esc(th.dataset.label) + (th.dataset.k === UI.sort ? `<span class="arr">${UI.dir > 0 ? '▲' : '▼'}</span>` : '');
  });

  const sig = JSON.stringify([list.map(s => [s.block_key, s.ping, s.tcp_ping, s.kbps, s.reliability, s.history, s.exit_ip, s.name]), UI.q, UI.cc, UI.proto, UI.sort, UI.dir]);
  if (sig !== rowSig){
    rowSig = sig;
    $('rows').innerHTML = list.length ? list.map((s, n) => {
      const hist = (s.history || []).map(v => `<i class="${v ? 'p' : 'f'}"></i>`).join('');
      return `<tr>
        <td class="muted">${n + 1}</td>
        <td><b>${esc(s.country || '??')}</b> <span class="muted">${esc(s.name || '')}</span></td>
        <td class="mono">${esc(s.front || s.block_key || '')}</td>
        <td class="mono ip" title="${esc(s.exit_ip || '')}">${esc(s.exit_ip || '—')}</td>
        <td class="${pingCls(s.ping)}"><b>${s.ping == null || s.ping < 0 ? '—' : s.ping + ' ms'}</b></td>
        <td class="muted">${s.tcp_ping == null || s.tcp_ping < 0 ? '—' : s.tcp_ping + ' ms'}</td>
        <td class="muted">${speed(s.kbps)}</td>
        <td><span class="hist" title="the last cycles, oldest first">${hist}</span><span class="${relCls(s.reliability)}">${s.reliability == null ? '—' : Math.round(s.reliability * 100) + '%'}</span></td>
        <td class="muted">${esc(s.protocol || '')}</td>
        <td><div class="rowact"><button class="btn sm" data-copy="${s._i}" title="Copy the share link">Copy</button><button class="btn sm danger" data-del="${esc(s.block_key || '')}">Delete</button></div></td>
      </tr>`; }).join('')
      : `<tr><td colspan="10" class="empty">${servers.length ? 'No server matches these filters.' : 'No servers yet — they appear after the next cycle.'}</td></tr>`;
  }
  const man = S.manual || [], blk = S.blocklist || [];
  $('sCount').textContent = `${list.length} of ${servers.length} shown` + (man.length ? ` · ${man.length} manual` : '') + (blk.length ? ` · ${blk.length} blocked` : '');
  $('manual').innerHTML = man.map((m, i) => `<div class="item"><span class="mono" title="${esc(m)}">${esc(m)}</span><button class="btn sm danger" data-unman="${i}">Remove</button></div>`).join('');
  $('bCount').textContent = blk.length ? '· ' + blk.length : '';
  $('blocked').innerHTML = blk.length
    ? blk.map((b, i) => `<div class="item"><span class="mono">${esc(typeof b === 'string' ? b : JSON.stringify(b))}</span><button class="btn sm" data-restore="${i}">Restore</button></div>`).join('')
    : '<div class="muted" style="font-size:12.5px">Nothing is blocked.</div>';
}

// ── history chart ─────────────────────────────────────────────────────────
function renderHistory(){
  const svg = $('chart'), cyc = ((H && H.cycles) || []).slice(-40);
  const W = Math.max(320, svg.clientWidth || 1000), Hh = 200;
  svg.setAttribute('viewBox', `0 0 ${W} ${Hh}`);
  if (!cyc.length){
    svg.innerHTML = `<text x="${W / 2}" y="100" fill="#6b7588" font-size="13" text-anchor="middle">No finished cycle in the log yet.</text>`;
    $('histNote').textContent = ''; return;
  }
  const pl = 38, pr = 44, pt = 14, pb = 24, n = cyc.length, bw = (W - pl - pr) / n;
  const maxP = Math.max(5, ...cyc.map(c => Math.max(c.published || 0, c.alive || 0)));
  const maxD = Math.max(60, ...cyc.map(c => c.duration || 0));
  const y = v => pt + (Hh - pt - pb) * (1 - v / maxP), yd = v => pt + (Hh - pt - pb) * (1 - v / maxD);
  let s = '<defs><linearGradient id="gp" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6c8cff"/><stop offset="1" stop-color="#8b6cff" stop-opacity=".7"/></linearGradient></defs>';
  for (let g = 0; g <= 4; g++){
    const v = maxP * g / 4, yy = y(v).toFixed(1);
    s += `<line x1="${pl}" x2="${W - pr}" y1="${yy}" y2="${yy}" stroke="#1d2230"/>` +
         `<text x="${pl - 8}" y="${+yy + 4}" fill="#6b7588" font-size="11" text-anchor="end">${Math.round(v)}</text>`;
  }
  s += `<text x="${W - pr + 8}" y="${pt + 4}" fill="#f3b44b" font-size="11">${Math.round(maxD)}s</text>` +
       `<text x="${W - pr + 8}" y="${Hh - pb}" fill="#f3b44b" font-size="11">0s</text>`;
  cyc.forEach((c, i) => {
    const x = pl + i * bw, w = Math.max(3, bw * .66), x0 = x + (bw - w) / 2, a = c.alive || 0, p = c.published || 0;
    if (a) s += `<rect x="${x0.toFixed(1)}" y="${y(a).toFixed(1)}" width="${w.toFixed(1)}" height="${(Hh - pb - y(a)).toFixed(1)}" rx="3" fill="rgba(47,211,156,.22)"/>`;
    s += c.ok === false
      ? `<rect x="${x0.toFixed(1)}" y="${Hh - pb - 8}" width="${w.toFixed(1)}" height="8" rx="2" fill="#ff6b7a"/>`
      : `<rect x="${(x0 + w * .16).toFixed(1)}" y="${y(p).toFixed(1)}" width="${(w * .68).toFixed(1)}" height="${(Hh - pb - y(p)).toFixed(1)}" rx="3" fill="url(#gp)"/>`;
  });
  s += `<polyline points="${cyc.map((c, i) => `${(pl + i * bw + bw / 2).toFixed(1)},${yd(c.duration || 0).toFixed(1)}`).join(' ')}" fill="none" stroke="#f3b44b" stroke-width="2" stroke-linejoin="round" opacity=".9"/>`;
  const span = (Date.parse((cyc[n - 1].ended || '').replace(' ', 'T') + 'Z') - Date.parse((cyc[0].ended || '').replace(' ', 'T') + 'Z')) / 3.6e6;
  const when = c => span > 20 ? (c.ended || '').slice(5, 16).replace('-', '/') : (c.ended || '').slice(11, 16);
  [0, Math.floor((n - 1) / 2), n - 1].filter((v, i, a) => a.indexOf(v) === i).forEach(i =>
    s += `<text x="${(pl + i * bw + bw / 2).toFixed(1)}" y="${Hh - 7}" fill="#6b7588" font-size="11" text-anchor="${i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'}">${when(cyc[i])} UTC</text>`);
  s += `<rect x="${pl}" y="0" width="${W - pl - pr}" height="${Hh}" fill="transparent" id="hit"/>`;
  svg.innerHTML = s;
  const avg = cyc.reduce((t, c) => t + (c.duration || 0), 0) / n;
  $('histNote').textContent = `${n} cycles · each ~${dur(avg)} · one every ~${dur(avgGap(cyc))}`;
  svg.onmousemove = e => {
    const r = svg.getBoundingClientRect(), fx = (e.clientX - r.left) / r.width * W, i = Math.floor((fx - pl) / bw);
    const tip = $('tip');
    if (i < 0 || i >= n){ tip.style.opacity = 0; return; }
    const c = cyc[i];
    tip.innerHTML = `<b>${esc((c.ended || '').replace('T', ' '))}</b><br>` +
      (c.ok === false ? '<span class="bad">not published</span><br>' : `published <b>${num(c.published)}</b><br>`) +
      `alive <b>${num(c.alive)}</b> of ${num(c.tested)} tested<br>collected ${num(c.collected)} · answered ${num(c.answered)}<br>` +
      `cycle ${dur(c.duration)}` + (c.errors ? ` · <span class="bad">${c.errors} errors</span>` : '') + (c.warnings ? ` · <span class="mid">${c.warnings} warnings</span>` : '');
    tip.style.left = Math.min(window.innerWidth - 240, e.clientX + 14) + 'px';
    tip.style.top = (e.clientY + 14) + 'px'; tip.style.opacity = 1;
  };
  svg.onmouseleave = () => { $('tip').style.opacity = 0; };
}
// The typical gap between two cycles: the median, because a single outage
// (a day with no cycle at all) would drag a mean anywhere.
function avgGap(cyc){
  const t = cyc.map(c => Date.parse((c.ended || '').replace(' ', 'T') + 'Z')).filter(x => !isNaN(x));
  if (t.length < 2) return null;
  const gaps = t.slice(1).map((x, i) => (x - t[i]) / 1000).sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}
function renderSources(){
  const src = (H && H.sources) || [], max = Math.max(1, ...src.map(s => s.alive));
  $('src').innerHTML = src.length ? src.map(s =>
    `<tr><td class="mono" title="${esc(s.name)}" style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${esc(s.name)}</td>` +
    `<td class="${s.alive ? 'good' : 'muted'}"><b>${num(s.alive)}</b></td><td class="muted">${num(s.tested)}</td>` +
    `<td style="width:90px"><div class="ybar"><i style="width:${(s.alive / max * 100).toFixed(0)}%"></i></div></td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">No source yields in the log yet.</td></tr>';
}

// ── logs ──────────────────────────────────────────────────────────────────
let logOff = null, logLines = [], carry = '', LVL = 'all', LQ = '';
const isErr = l => / (ERROR|CRITICAL) /.test(l), isWarn = l => / WARNING /.test(l);
async function pullLog(){
  const r = await api('/api/logs' + (logOff == null ? '?n=1200' : '?since=' + logOff));
  const d = await r.json();
  if (d.reset){ logLines = (d.text || '').split('\n'); carry = ''; }
  else if (d.text){
    const parts = (carry + d.text).split('\n'); carry = parts.pop(); logLines.push(...parts);
  }
  if (logLines.length > 6000) logLines = logLines.slice(-6000);
  const changed = d.reset || !!d.text; logOff = d.offset;
  if (changed) renderLog();
}
function renderLog(){
  const el = $('log'), q = LQ.toLowerCase();
  let lines = logLines;
  if (LVL === 'warn') lines = lines.filter(l => isWarn(l) || isErr(l));
  else if (LVL === 'err') lines = lines.filter(isErr);
  if (q) lines = lines.filter(l => l.toLowerCase().includes(q));
  lines = lines.slice(-1500);
  el.innerHTML = lines.length ? lines.map(l => {
    const m = l.match(/^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d)(.*)$/);
    const body = m ? `<span class="t">${m[1]}</span>${esc(m[2])}` : esc(l);
    return isErr(l) ? `<span class="e">${body}</span>` : isWarn(l) ? `<span class="w">${body}</span>` : body;
  }).join('\n') : '<span class="t">No line matches.</span>';
  if ($('follow').checked) el.scrollTop = el.scrollHeight;
}

// ── loops ─────────────────────────────────────────────────────────────────
async function refresh(){
  const r = await api('/api/status'); S = await r.json(); fetchedAt = Date.now();
  renderTop(); renderKpis(); renderCycle(); renderSystem(); renderServers();
}
async function refreshHistory(){
  const r = await api('/api/history'); H = await r.json();
  renderHistory(); renderSources(); if (S){ renderKpis(); renderCycle(); }
}
function every(fn, ms){
  const tick = async () => {
    if (!document.hidden){
      try { await fn(); setConn(true); } catch (e) { if (e.message !== 'signed out') setConn(false); }
    }
    setTimeout(tick, ms);
  };
  tick();
}
every(refresh, 5000);
every(refreshHistory, 30000);
every(pullLog, 3000);
setInterval(() => { if (S) renderTop(); }, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden){ refresh().catch(() => {}); pullLog().catch(() => {}); } });
let rz; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(renderHistory, 150); });

// ── actions ───────────────────────────────────────────────────────────────
$('q').addEventListener('input', e => { UI.q = e.target.value; renderServers(); });
$('fProto').addEventListener('change', e => { UI.proto = e.target.value; saveUI(); renderServers(); });
$('bClear').onclick = () => { UI.q = ''; UI.cc = ''; UI.proto = ''; $('q').value = ''; saveUI(); renderServers(); };
$('ccChips').addEventListener('click', e => {
  const c = e.target.closest('[data-cc]'); if (!c) return;
  UI.cc = UI.cc === c.dataset.cc ? '' : c.dataset.cc; saveUI(); renderServers();
});
document.querySelector('thead').addEventListener('click', e => {
  const th = e.target.closest('th[data-k]'); if (!th) return;
  if (UI.sort === th.dataset.k) UI.dir = -UI.dir; else { UI.sort = th.dataset.k; UI.dir = th.dataset.k === 'reliability' || th.dataset.k === 'kbps' ? -1 : 1; }
  saveUI(); renderServers();
});
document.addEventListener('keydown', e => {
  if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){ e.preventDefault(); $('q').focus(); }
  if (e.key === 'Escape' && $('modal').classList.contains('show')) $('mNo').click();
});
$('rows').addEventListener('click', async e => {
  const cp = e.target.closest('[data-copy]'), del = e.target.closest('[data-del]');
  if (cp){
    const s = (S.servers || [])[+cp.dataset.copy];
    toast(s && s.config && await copyText(s.config) ? 'Share link copied' : 'Could not copy');
  }
  if (del){
    const key = del.dataset.del;
    if (!await ask('Delete this server?', key + '\n\nIt is blocked from now on and removed from the published list right away.', 'Delete', true)) return;
    try { const d = await post('/api/delete', {block_key: key}); toast(d.ok ? 'Deleted · ' + d.message : 'Delete failed · ' + (d.message || d.error)); }
    catch (_) { toast('Delete failed'); }
    refresh().catch(() => {});
  }
});
$('blocked').addEventListener('click', async e => {
  const b = e.target.closest('[data-restore]'); if (!b) return;
  const key = (S.blocklist || [])[+b.dataset.restore];
  if (!await ask('Restore this server?', String(key) + '\n\nIt may be tested and published again from the next cycle.', 'Restore')) return;
  try { await post('/api/restore', {block_key: key}); toast('Restored'); } catch (_) { toast('Restore failed'); }
  refresh().catch(() => {});
});
$('manual').addEventListener('click', async e => {
  const b = e.target.closest('[data-unman]'); if (!b) return;
  try { await post('/api/remove_manual', {link: (S.manual || [])[+b.dataset.unman]}); toast('Removed'); } catch (_) { toast('Remove failed'); }
  refresh().catch(() => {});
});
$('bAdd').onclick = async () => {
  const el = $('addLink'), link = el.value.trim();
  if (!link){ toast('Paste a vmess / vless / trojan / ss link first'); return; }
  try { const d = await post('/api/add', {link}); toast(d.ok ? 'Added · ' + d.message : 'Not added · ' + (d.message || d.error)); if (d.ok){ el.value = ''; refresh().catch(() => {}); } }
  catch (_) { toast('Add failed'); }
};
$('addLink').addEventListener('keydown', e => { if (e.key === 'Enter') $('bAdd').click(); });
$('bRestart').onclick = async () => {
  if (!await ask('Start a new cycle now?', 'The collector restarts: the cycle in progress is dropped and a fresh one starts straight away.', 'Restart')) return;
  try { const d = await post('/api/restart'); toast(d.ok ? d.message : 'Failed · ' + d.message); } catch (_) { toast('Restart failed'); }
  setTimeout(() => refresh().catch(() => {}), 2500);
};
$('bUpdate').onclick = async () => {
  if (!await ask('Update the server?', 'Stops both services, pulls the latest code, reinstalls dependencies and restarts. The console is back in about a minute.', 'Update')) return;
  $('bUpdate').disabled = true;
  try { const d = await post('/api/update'); toast(d.ok ? 'Update started — services restarting…' : 'Update failed · ' + (d.message || d.error)); }
  catch (_) { toast('Update started (the connection dropped, as expected).'); }
  setTimeout(() => { $('bUpdate').disabled = false; }, 15000);
};
$('lvl').addEventListener('click', e => {
  const b = e.target.closest('button[data-l]'); if (!b) return;
  LVL = b.dataset.l; document.querySelectorAll('#lvl button').forEach(x => x.classList.toggle('on', x === b)); renderLog();
});
$('lq').addEventListener('input', e => { LQ = e.target.value; renderLog(); });
$('follow').addEventListener('change', () => { if ($('follow').checked) renderLog(); });
$('log').addEventListener('scroll', () => {
  const el = $('log'), atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
  if ($('follow').checked !== atEnd) $('follow').checked = atEnd;
});
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
