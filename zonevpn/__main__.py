"""Entry point: runs the collect/test/publish cycle on a fixed interval.

The process stays alive (systemd keeps it running) and schedules itself so that
each cycle starts every `interval_minutes`, regardless of how long the previous
cycle took. A single cycle that overruns the interval simply starts the next one
immediately. Any error in a cycle is logged and swallowed so the loop never dies.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from . import config as cfgmod
from . import state
from .geo import GeoResolver
from .migrate import migrate_config
from .runner import run_cycle

_fmt = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")

# Also write to a rotating file so the dashboard can show "live logs" without
# needing journald permissions.
try:
    state.ensure_dir()
    _fh = RotatingFileHandler(state.LOG_FILE, maxBytes=1_000_000, backupCount=2,
                              encoding="utf-8")
    _fh.setFormatter(_fmt)
    logging.getLogger().addHandler(_fh)
except Exception:  # pragma: no cover - never let logging setup kill the app
    pass

log = logging.getLogger("zonevpn")

_stop = asyncio.Event()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handler():
        log.info("shutdown signal received")
        _stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: _stop.set())


async def run_single() -> None:
    # Self-heal the config structure first, so a freshly-pulled build never runs
    # on a stale config that's missing newly-added settings.
    try:
        migrate_config()
    except Exception:
        log.exception("config migration failed (continuing)")
    cfg = cfgmod.load()
    if not cfg.get("github_token") or not cfg.get("gist_id"):
        raise SystemExit("github_token and gist_id must be set in config.json "
                         "(run: python setup_wizard.py)")
    xray_path = cfgmod.find_xray(cfg.get("xray_path", "auto"))
    geo = GeoResolver(cfgmod.resolve_geoip_db(cfg))
    log.info("running a single cycle (--once)")
    await run_cycle(cfg, xray_path, geo)


async def main_loop() -> None:
    try:
        if migrate_config():
            log.info("config.json was auto-migrated to the latest structure")
    except Exception:
        log.exception("config migration failed (continuing)")
    cfg = cfgmod.load()
    if not cfg.get("github_token") or not cfg.get("gist_id"):
        raise SystemExit("github_token and gist_id must be set in config.json "
                         "(run: python setup_wizard.py)")

    xray_path = cfgmod.find_xray(cfg.get("xray_path", "auto"))
    geo = GeoResolver(cfgmod.resolve_geoip_db(cfg))
    interval = max(1, int(cfg.get("interval_minutes", 10))) * 60

    log.info("ZoneVPN started | xray=%s | interval=%dmin | sources=%d",
             xray_path, interval // 60, len(cfg.get("sources", [])))

    _install_signal_handlers(asyncio.get_running_loop())

    while not _stop.is_set():
        start = time.monotonic()
        try:
            await run_cycle(cfg, xray_path, geo)
        except Exception:
            log.exception("cycle failed")
        elapsed = time.monotonic() - start
        wait = max(0.0, interval - elapsed)
        if wait == 0:
            log.warning("cycle took %.0fs, longer than the %ds interval", elapsed, interval)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass

    log.info("ZoneVPN stopped")


def main() -> None:
    once = "--once" in sys.argv
    try:
        asyncio.run(run_single() if once else main_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
