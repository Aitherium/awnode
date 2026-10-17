"""
awnode update check
===================

Once-a-day PyPI lookup for a newer ``awnode``. awnode had none: a node kept
running whatever ``pip install awnode`` fetched the day it was set up, and
nothing on the CLI or on ``GET /status`` said a release existed.

- The newest version is cached in $AITHER_HOME/update-check-awnode.json
  (default ~/.aither); a lookup happens at most once per CHECK_INTERVAL.
  The attempt is stamped before the lookup starts, so a node that cannot
  reach pypi.org is not re-asked on every /status poll either.
- Never blocks: the lookup runs on a daemon thread. The CLI prints a one-line
  notice from the cache at start, or when a fresh lookup lands; a short
  command waits at most EXIT_WAIT seconds at exit for a lookup it started.
- ``GET /status`` reports :func:`status` (cache only, never the network) and
  kicks a background refresh when the cache is stale.
- Off with AWNODE_NO_UPDATE_CHECK=1 (or the fleet-wide AITHER_NO_UPDATE_CHECK=1),
  and on an offline box (AITHER_OFFLINE=1).
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from awnode import __version__

PACKAGE = "awnode"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_INTERVAL = 86400  # 24 hours
FETCH_TIMEOUT = 3.0
#: Longest a short CLI command lingers at exit for a lookup it started.
EXIT_WAIT = 1.0

_TRUTHY = ("1", "true", "yes", "on")
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def disabled(env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return any(
        (env.get(k) or "").strip().lower() in _TRUTHY
        for k in ("AWNODE_NO_UPDATE_CHECK", "AITHER_NO_UPDATE_CHECK", "AITHER_OFFLINE")
    )


def cache_path() -> Path:
    home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    return home / "update-check-awnode.json"


def _parse_version(v: str) -> Tuple[int, ...]:
    clean = v.lstrip("v").split("-")[0].split("+")[0]
    parts = []
    for p in clean.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def is_newer(latest: str, current: str) -> bool:
    """True only when `latest` is strictly newer: a dev build ahead of PyPI is never told to downgrade."""
    return bool(latest and current) and _parse_version(latest) > _parse_version(current)


def upgrade_command(prefix: Optional[str] = None, in_container: Optional[bool] = None) -> str:
    """How THIS install upgrades: the container image, pipx, `uv tool`, or pip."""
    if in_container is None:
        # /.dockerenv is Docker's marker, /run/.containerenv podman's.
        in_container = (
            (os.environ.get("AWNODE_IN_CONTAINER") or "").strip().lower() in _TRUTHY
            or Path("/.dockerenv").exists()
            or Path("/run/.containerenv").exists()
        )
    if in_container:
        return "pull/rebuild the awnode image (pip inside the container is lost on restart)"
    p = (prefix if prefix is not None else sys.prefix).replace("\\", "/").lower()
    if "/pipx/venvs/" in p:
        return f"pipx upgrade {PACKAGE}"
    if "/uv/tools/" in p:
        return f"uv tool upgrade {PACKAGE}"
    return f"pip install --upgrade {PACKAGE}"


def _load_cache() -> Optional[Dict[str, Any]]:
    """The awnode cache record whatever it holds, including an attempt-only one."""
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("package") == PACKAGE:
            return data
    except Exception:
        pass
    return None


def read_cache() -> Optional[Dict[str, Any]]:
    """The cached lookup, only when it holds a version (not just an attempt stamp)."""
    data = _load_cache()
    return data if data and data.get("latest_version") else None


def _save(data: Dict[str, Any]) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _stamp_attempt(prev: Optional[Dict[str, Any]]) -> None:
    """Record that a lookup is starting, keeping any older latest_version.

    Without this a failed lookup left no trace, so every /status poll on a
    node without egress started another pypi.org request.
    """
    data = dict(prev or {})
    data["package"] = PACKAGE
    data["attempted_at"] = time.time()
    _save(data)


def _write_cache(latest: str) -> None:
    now = time.time()
    _save({
        "package": PACKAGE,
        "latest_version": latest,
        "checked_at": now,
        "attempted_at": now,
    })


def _fresh(cached: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    """True when PyPI was asked -- successfully or not -- within CHECK_INTERVAL."""
    if not cached:
        return False
    now = time.time() if now is None else now
    stamps = []
    for k in ("checked_at", "attempted_at"):
        try:
            stamps.append(float(cached.get(k) or 0))
        except (TypeError, ValueError):
            pass
    return now - max(stamps or [0.0]) < CHECK_INTERVAL


def fetch_latest() -> Optional[str]:
    """The newest awnode on PyPI, or None on any failure."""
    try:
        from urllib.request import urlopen
        with urlopen(PYPI_URL, timeout=FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return (data.get("info") or {}).get("version") or None
    except Exception:
        return None


def status(current: str = __version__) -> Dict[str, Any]:
    """Update state for GET /status -- from the cache only, never the network."""
    if disabled():
        return {"enabled": False, "current": current}
    cached = read_cache()
    latest = (cached or {}).get("latest_version")
    out: Dict[str, Any] = {
        "enabled": True,
        "current": current,
        "latest": latest,
        "update_available": is_newer(latest or "", current),
        "checked_at": (cached or {}).get("checked_at"),
    }
    if out["update_available"]:
        out["upgrade_command"] = upgrade_command()
    return out


def notice(current: str = __version__) -> Optional[str]:
    s = status(current)
    if s.get("update_available"):
        return f"awnode {s['latest']} is available (you have {current}). Run: {s['upgrade_command']}"
    return None


def refresh_async(on_done=None) -> Optional[threading.Thread]:
    """Start a background PyPI lookup when the cache is stale; never blocks.

    Returns the thread it started, or None when disabled / asked within the
    interval (a failed ask counts) / one is already running. `on_done` runs on that thread after a successful lookup.
    """
    global _thread
    if disabled() or _fresh(_load_cache()):
        return None
    with _lock:
        if _thread is not None and _thread.is_alive():
            return None
        _stamp_attempt(_load_cache())

        def _run() -> None:
            latest = fetch_latest()
            if latest:
                _write_cache(latest)
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:
                        pass

        _thread = threading.Thread(target=_run, name="awnode-update-check", daemon=True)
        _thread.start()
        return _thread


def _print_notice() -> None:
    msg = notice()
    if msg:
        print(f"[awnode] {msg}", file=sys.stderr)


def cli_start_check() -> None:
    """CLI start: print a cached notice now, refresh in the background if stale.

    A cached "update available" prints immediately. A stale cache starts a
    lookup that prints its own notice when it lands (long-running `start`), and
    a short command gives it at most EXIT_WAIT seconds at exit. Never raises.
    """
    try:
        if disabled():
            return
        if _fresh(_load_cache()):
            _print_notice()
            return
        t = refresh_async(on_done=_print_notice)
        if t is not None:
            atexit.register(t.join, EXIT_WAIT)
    except Exception:
        pass  # an update check must never stop the node CLI
