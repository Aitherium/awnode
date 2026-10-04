#!/usr/bin/env python3
"""awnode hub -- ONE local MCP endpoint for every aw* brick on this machine.

Why this exists (2026-09-27)
----------------------------
Every Claude Code session spawned its own copy of every brick's stdio MCP
server: ``awgraph mcp``, ``awrelay mcp``, ``awdecide mcp``, ``awfocus mcp``,
``awm mcp``, ``awprism mcp``, ``awfind mcp``, ``python -m awgit.mcp_server``,
``python -m adk.harnesses.mcp_stdio`` -- and on Windows each ``aw*.exe`` is a
launcher plus a python.exe. Measured on the owner's host: 17 sessions, 268
python.exe, CPU pinned at 100 %, the terminal unresponsive.

The hub runs ONCE per machine on 127.0.0.1 and speaks Streamable-HTTP MCP
(plain JSON responses). It owns the brick servers as stdio CHILDREN, started
lazily on the first call that needs them and reaped when idle. A session
reaches it through ``mcp_stdio_bridge.py --hub``, which is the one process a
session spawns.

Semantics kept from the per-session layout
------------------------------------------
* **Project directory.** Several bricks read the repo from their cwd /
  ``CLAUDE_PROJECT_DIR`` (awgit leases, awm's default scope, awgraph's index).
  The bridge sends ``X-Aw-Project-Dir``; children are keyed by
  ``(brick, project dir)`` and started with that cwd and env var. Sessions in
  the same checkout share one child per brick.
* **Tool names are preserved.** A brick's tools are served under their own
  names. Only a collision BETWEEN bricks renames the later one to
  ``<brick>__<tool>`` (brick order below decides; the mapping is served on
  ``GET /health``).

Security
--------
Loopback only. Every ``/mcp`` request needs ``X-Aw-Hub-Token`` equal to the
token in ``~/.aither/aw-hub/token`` (created 0600 on first start), and any
request carrying an ``Origin`` header is refused -- a browser page cannot
drive the bricks through a DNS-rebound name.

Usage
-----
    python -m awnode.hub serve [--port 47933]    # or: awnode hub serve
    python -m awnode.hub status                  # exit 0 up, 1 down
    python -m awnode.hub install | uninstall     # Windows logon task
    python -m awnode.hub --self-test             # 0 pass, 1 fail, 2 cannot run

Stdlib only, Python 3.10+: the file also runs as a plain script
(``python awnode/awnode/hub.py serve``), which is how the bridge starts it
from a checkout whose awnode is not installed.
"""
from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_CACHE_LOCK = threading.Lock()

HUB_NAME = "aw-hub"
HUB_VERSION = "1.0"
DEFAULT_PORT = int(os.environ.get("AW_HUB_PORT", "47933"))
HOST = "127.0.0.1"
STATE_DIR = Path(os.environ.get("AW_HUB_HOME") or (Path.home() / ".aither" / "aw-hub"))
TOKEN_HEADER = "X-Aw-Hub-Token"
PROJECT_HEADER = "X-Aw-Project-Dir"
PROTOCOL = "2025-06-18"
TASK_NAME = "awnode MCP hub"

# Default brick set, in collision-precedence order. ``main`` = "<module>:<func>"
# run with ``args`` as argv (exactly what the console script does, minus the
# Windows .exe launcher process); ``module`` = ``python -m <module>``.
DEFAULT_BRICKS: List[Dict[str, Any]] = [
    {"name": "awgit", "module": "awgit.mcp_server"},
    {"name": "awm", "main": "awm.cli:main", "args": ["mcp"]},
    {"name": "awrelay", "main": "awrelay.cli:main", "args": ["mcp"]},
    {"name": "awgraph", "main": "awgraph.cli:main", "args": ["mcp"]},
    {"name": "awfocus", "main": "awfocus.cli:main", "args": ["mcp"]},
    {"name": "awdecide", "main": "awdecide.cli:main", "args": ["mcp"]},
    {"name": "awfind", "main": "awfind.cli:main", "args": ["mcp"]},
    {"name": "awprism", "main": "awprism.cli:main", "args": ["mcp"]},
    {"name": "awsh", "module": "adk.harnesses.mcp_stdio"},
]

_CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    """stderr; the serve loop redirects it to the hub log."""
    try:
        sys.stderr.write("[aw-hub %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()
    except (OSError, ValueError):
        return  # no stderr left to report to


# ── config ─────────────────────────────────────────────────────────────────


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    """``{"bricks": {<name>: {"env": {...}, "disabled": bool, ...}}, "extra": [...]}``.

    A missing file is the default config; an unreadable one is logged and
    ignored (the hub must come up).
    """
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log("config %s unreadable (%s); using defaults" % (path, exc))
        return {}
    return data if isinstance(data, dict) else {}


def brick_specs(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Default bricks with per-brick overrides applied, disabled ones dropped."""
    overrides = config.get("bricks") if isinstance(config.get("bricks"), dict) else {}
    out: List[Dict[str, Any]] = []
    for base in DEFAULT_BRICKS + list(config.get("extra") or []):
        if not isinstance(base, dict) or not base.get("name"):
            continue
        spec = {**base, **(overrides.get(base["name"]) or {})}
        if spec.get("disabled"):
            continue
        out.append(spec)
    return out


def brick_argv(spec: Dict[str, Any], python: str = sys.executable) -> Optional[List[str]]:
    """The argv that runs ``spec``'s stdio server, or None when not installed."""
    if spec.get("argv"):
        return [str(a) for a in spec["argv"]]
    module = spec.get("module")
    main = spec.get("main")
    top = (module or main or "").split(":")[0].split(".")[0]
    try:
        present = bool(top) and importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        present = False
    if not present:
        exe = shutil.which(spec["name"])
        return [exe, *[str(a) for a in spec.get("args") or ["mcp"]]] if exe else None
    if module:
        return [python, "-m", module, *[str(a) for a in spec.get("args") or []]]
    mod, _, func = str(main).partition(":")
    argv0 = spec["name"]
    code = (
        "import sys; from %s import %s as _m; sys.argv=%r; sys.exit(_m())"
        % (mod, func or "main", [argv0, *[str(a) for a in spec.get("args") or []]])
    )
    return [python, "-c", code]


def norm_dir(raw: Optional[str], fallback: str) -> str:
    """A project dir the hub may start a child in: existing, absolute, normalised."""
    if raw:
        try:
            p = os.path.abspath(raw)
            if os.path.isdir(p):
                return os.path.normcase(p) if os.name == "nt" else p
        except (OSError, ValueError):
            return fallback  # an unusable path is the default dir, never a crash
    return fallback


# ── one brick child ────────────────────────────────────────────────────────


class ChildError(Exception):
    """The brick child failed to start, answer, or stay alive."""


class BrickChild:
    """A brick's stdio MCP server, owned by the hub, shared by every session."""

    def __init__(self, name: str, argv: List[str], cwd: str, env: Dict[str, str],
                 log_dir: Path, start_timeout: float = 90.0):
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.log_dir = log_dir
        self.start_timeout = start_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.write_lock = threading.Lock()
        self.pending: Dict[int, Tuple[threading.Event, List[dict]]] = {}
        self.pending_lock = threading.Lock()
        self.next_id = 1
        self.last_used = time.monotonic()
        self.tools: List[dict] = []
        self.on_tools_changed: Optional[Callable[[], None]] = None

    # wire -------------------------------------------------------------------
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, msg: dict) -> None:
        if not self.alive():
            raise ChildError("%s is not running" % self.name)
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self.write_lock:
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ChildError("%s stdin closed: %s" % (self.name, exc)) from exc

    def _reader(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # a brick printing a banner on stdout is its bug, not a crash
            if not isinstance(msg, dict):
                continue
            if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
                with self.pending_lock:
                    slot = self.pending.pop(msg.get("id"), None)  # type: ignore[arg-type]
                if slot is not None:
                    slot[1].append(msg)
                    slot[0].set()
                continue
            method = msg.get("method")
            if method and "id" in msg:
                # server->client request (roots/sampling/elicitation): the hub
                # advertised no client capabilities, so refuse it plainly.
                try:
                    self._write({"jsonrpc": "2.0", "id": msg["id"],
                                 "error": {"code": -32601,
                                           "message": "aw-hub client supports no %s" % method}})
                except ChildError:
                    break
                continue
            if method == "notifications/tools/list_changed":
                threading.Thread(target=self._refresh_tools_quiet, daemon=True).start()
        # EOF: fail every waiter now instead of letting it time out.
        with self.pending_lock:
            waiters = list(self.pending.values())
            self.pending.clear()
        for ev, box in waiters:
            box.append({"error": {"code": -32000,
                                  "message": "%s exited mid-request" % self.name}})
            ev.set()

    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = 600.0) -> dict:
        """One JSON-RPC request; returns the response message (result or error)."""
        with self.pending_lock:
            rid = self.next_id
            self.next_id += 1
            ev = threading.Event()
            box: List[dict] = []
            self.pending[rid] = (ev, box)
        msg: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
        except ChildError:
            with self.pending_lock:
                self.pending.pop(rid, None)
            raise
        if not ev.wait(timeout):
            with self.pending_lock:
                self.pending.pop(rid, None)
            raise ChildError("%s did not answer %s within %.0fs" % (self.name, method, timeout))
        self.last_used = time.monotonic()
        return box[0]

    # lifecycle --------------------------------------------------------------
    def start(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        err = open(self.log_dir / ("%s.log" % self.name), "ab")  # noqa: SIM115 -- owned by the child
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, **kwargs)
        except OSError as exc:
            raise ChildError("%s could not start: %s" % (self.name, exc)) from exc
        finally:
            err.close()  # the child holds its own handle
        threading.Thread(target=self._reader, daemon=True,
                         name="brick-%s" % self.name).start()
        resp = self.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": HUB_NAME, "version": HUB_VERSION}},
            timeout=self.start_timeout)
        if "result" not in resp:
            self.stop()
            raise ChildError("%s initialize failed: %s" % (self.name, json.dumps(resp)[:200]))
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.refresh_tools()

    def refresh_tools(self) -> List[dict]:
        tools: List[dict] = []
        cursor = None
        for _ in range(20):
            resp = self.request("tools/list", {"cursor": cursor} if cursor else {},
                                timeout=self.start_timeout)
            result = resp.get("result") or {}
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        changed = tools != self.tools
        self.tools = tools
        if changed and self.on_tools_changed:
            self.on_tools_changed()
        return tools

    def _refresh_tools_quiet(self) -> None:
        try:
            self.refresh_tools()
        except ChildError as exc:
            log("tools refresh for %s failed: %s" % (self.name, exc))

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError as exc:
            log("%s stdin already closed: %s" % (self.name, exc))
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── the hub ────────────────────────────────────────────────────────────────


class Hub:
    """Routes MCP requests to brick children keyed by (brick, project dir)."""

    def __init__(self, specs: List[Dict[str, Any]], state_dir: Path,
                 default_dir: Optional[str] = None, idle_ttl: float = 900.0,
                 max_dirs: int = 16, child_factory: Optional[Callable[..., Any]] = None):
        self.specs = {s["name"]: s for s in specs}
        self.order = [s["name"] for s in specs]
        self.state_dir = state_dir
        self.default_dir = norm_dir(default_dir, os.path.normcase(str(Path.home())))
        self.idle_ttl = idle_ttl
        self.max_dirs = max_dirs
        self.child_factory = child_factory or BrickChild
        self.children: Dict[Tuple[str, str], Any] = {}
        self.child_locks: Dict[Tuple[str, str], threading.Lock] = {}
        self.lock = threading.Lock()
        self.cache_path = state_dir / "tools-cache.json"
        self.tool_cache: Dict[str, List[dict]] = self._load_cache()
        self.errors: Dict[str, str] = {}
        self.started_at = time.time()
        self.stop_event = threading.Event()

    # tool cache ---------------------------------------------------------------
    def _load_cache(self) -> Dict[str, List[dict]]:
        try:
            doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        bricks = doc.get("bricks") if isinstance(doc, dict) else None
        return {k: v for k, v in (bricks or {}).items() if isinstance(v, list)}

    def _save_cache(self) -> None:
        # Bricks start concurrently and each saves the cache. With one shared
        # ".tmp" name two writers collided on Windows (WinError 32 / Errno 13) and
        # the cache silently kept the older copy. Serialise, use a per-write tmp
        # name, and retry the rename briefly while a reader holds the target.
        with _CACHE_LOCK:
            tmp = self.cache_path.with_name(
                "tools-cache.%d.%d.tmp" % (os.getpid(), threading.get_ident()))
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"ts": time.time(), "bricks": self.tool_cache}),
                               encoding="utf-8")
                for attempt in range(5):
                    try:
                        os.replace(tmp, self.cache_path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except OSError as exc:
                log("tool cache write failed (non-fatal): %s" % exc)
                try:
                    tmp.unlink()
                except OSError:
                    log("stale tool cache tmp left behind: %s" % tmp)

    # children -----------------------------------------------------------------
    def _env_for(self, spec: Dict[str, Any], project_dir: str) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
        env["CLAUDE_PROJECT_DIR"] = project_dir
        env["AW_HUB_CHILD"] = "1"
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def child(self, brick: str, project_dir: str) -> Any:
        """The live child for (brick, dir), starting it if needed."""
        key = (brick, project_dir)
        with self.lock:
            klock = self.child_locks.setdefault(key, threading.Lock())
        with klock:
            existing = self.children.get(key)
            if existing is not None and existing.alive():
                existing.last_used = time.monotonic()
                return existing
            spec = self.specs.get(brick)
            if spec is None:
                raise ChildError("no brick named %s" % brick)
            argv = brick_argv(spec)
            if argv is None:
                self.errors[brick] = ("%s is not installed on this machine (pip install %s)"
                                      % (brick, brick))
                raise ChildError(self.errors[brick])
            self._evict_dirs(project_dir)
            c = self.child_factory(brick, argv, project_dir, self._env_for(spec, project_dir),
                                   self.state_dir / "logs")
            c.on_tools_changed = lambda b=brick, ch=c: self._learn(b, ch.tools)
            try:
                c.start()
            except ChildError as exc:
                self.errors[brick] = str(exc)
                raise
            self.errors.pop(brick, None)
            self.children[key] = c
            self._learn(brick, c.tools)
            log("started %s for %s (pid %s)" % (brick, project_dir,
                                                getattr(getattr(c, "proc", None), "pid", "?")))
            return c

    def _learn(self, brick: str, tools: List[dict]) -> None:
        if tools and self.tool_cache.get(brick) != tools:
            self.tool_cache[brick] = list(tools)
            self._save_cache()

    def _evict_dirs(self, incoming: str) -> None:
        """Bound the number of distinct project dirs holding children (LRU)."""
        dirs: Dict[str, float] = {}
        for (_, d), c in self.children.items():
            dirs[d] = max(dirs.get(d, 0.0), c.last_used)
        if incoming in dirs or len(dirs) < self.max_dirs:
            return
        oldest = min(dirs, key=lambda d: dirs[d])
        for key in [k for k in self.children if k[1] == oldest]:
            self.children.pop(key).stop()
        log("evicted children for %s (max_dirs=%d)" % (oldest, self.max_dirs))

    def reap_idle(self, now: Optional[float] = None) -> int:
        now = time.monotonic() if now is None else now
        dead = [k for k, c in list(self.children.items())
                if not c.alive() or now - c.last_used > self.idle_ttl]
        for key in dead:
            c = self.children.pop(key, None)
            if c is not None:
                c.stop()
        return len(dead)

    def reaper_loop(self) -> None:
        while not self.stop_event.wait(60.0):
            n = self.reap_idle()
            if n:
                log("reaped %d idle brick child(ren)" % n)

    def shutdown(self) -> None:
        self.stop_event.set()
        for c in list(self.children.values()):
            c.stop()
        self.children.clear()

    # routing ------------------------------------------------------------------
    def routes(self) -> Tuple[List[dict], Dict[str, Tuple[str, str]]]:
        """(served tool list, exposed-name -> (brick, original name))."""
        tools: List[dict] = []
        table: Dict[str, Tuple[str, str]] = {}
        for brick in self.order:
            for t in self.tool_cache.get(brick) or []:
                orig = t.get("name")
                if not isinstance(orig, str) or not orig:
                    continue
                exposed = orig if orig not in table else "%s__%s" % (brick, orig)
                if exposed in table:
                    continue
                table[exposed] = (brick, orig)
                tools.append(t if exposed == orig else {**t, "name": exposed})
        return tools, table

    def ensure_tool_cache(self, project_dir: str, timeout: float = 90.0) -> None:
        """Learn the tool list of every brick not yet in the cache (in parallel)."""
        missing = [b for b in self.order if not self.tool_cache.get(b)
                   and b not in self.errors]
        if not missing:
            return
        threads = []
        for brick in missing:
            th = threading.Thread(target=self._try_child, args=(brick, project_dir), daemon=True)
            th.start()
            threads.append(th)
        deadline = time.monotonic() + timeout
        for th in threads:
            th.join(max(0.0, deadline - time.monotonic()))

    def _try_child(self, brick: str, project_dir: str) -> None:
        try:
            self.child(brick, project_dir)
        except ChildError as exc:
            log("brick %s unavailable: %s" % (brick, exc))

    def handle(self, msg: dict, project_dir: str) -> Optional[dict]:
        """One JSON-RPC message in, the response out (None for notifications)."""
        method = msg.get("method")
        rid = msg.get("id")
        if not isinstance(method, str):
            return None
        if rid is None:
            return None  # notifications: nothing to forward to shared children
        try:
            result = self._dispatch(method, msg.get("params") or {}, project_dir)
        except ChildError as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 -- one bad call must not kill the hub
            log("handler error for %s: %s" % (method, exc))
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": "aw-hub error: %s" % exc}}
        if isinstance(result, dict) and "__error__" in result:
            return {"jsonrpc": "2.0", "id": rid, "error": result["__error__"]}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _dispatch(self, method: str, params: dict, project_dir: str) -> dict:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": HUB_NAME, "version": HUB_VERSION},
                "instructions": "aw* bricks served by one local hub: " + ", ".join(self.order),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            self.ensure_tool_cache(project_dir)
            return {"tools": self.routes()[0]}
        if method in ("prompts/list", "resources/list", "resources/templates/list"):
            key = {"prompts/list": "prompts", "resources/list": "resources",
                   "resources/templates/list": "resourceTemplates"}[method]
            return {key: []}
        if method == "tools/call":
            name = params.get("name")
            _, table = self.routes()
            if name not in table:
                self.ensure_tool_cache(project_dir)
                _, table = self.routes()
            if name not in table:
                return {"__error__": {"code": -32602, "message": "unknown tool %r" % name}}
            brick, orig = table[name]
            child = self.child(brick, project_dir)
            fwd = {**params, "name": orig}
            resp = child.request("tools/call", fwd)
            if "error" in resp:
                return {"__error__": resp["error"]}
            return resp.get("result") or {}
        return {"__error__": {"code": -32601, "message": "aw-hub: no method %s" % method}}

    def health(self) -> dict:
        tools, table = self.routes()
        renamed = {k: "%s:%s" % v for k, v in table.items() if k != v[1]}
        return {
            "ok": True, "name": HUB_NAME, "version": HUB_VERSION, "pid": os.getpid(),
            "uptime_s": int(time.time() - self.started_at),
            "bricks": self.order,
            "tools": {b: len(self.tool_cache.get(b) or []) for b in self.order},
            "renamed": renamed,
            "children": [{"brick": b, "dir": d, "alive": c.alive(),
                          "idle_s": int(time.monotonic() - c.last_used)}
                         for (b, d), c in self.children.items()],
            "errors": dict(self.errors),
            "tool_count": len(tools),
        }


# ── HTTP transport ─────────────────────────────────────────────────────────


def read_or_create_token(state_dir: Path) -> str:
    path = state_dir / "token"
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except OSError as exc:
        log("no hub token yet (%s); minting one" % exc.__class__.__name__)
    state_dir.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    path.write_text(tok, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        log("chmod 600 on the token failed (%s); it stays in the user profile" % exc)
    return tok


def make_handler(hub: Hub, token: str, port: int) -> type:
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "aw-hub/" + HUB_VERSION

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return  # per-request logging would be the CPU storm again

        def _send(self, code: int, body: Optional[dict], headers: Optional[dict] = None) -> None:
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(code)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _guard(self, need_token: bool) -> bool:
            if self.headers.get("Origin"):
                self._send(403, {"error": "browser origins are refused"})
                return False
            if (self.headers.get("Host") or "") not in allowed_hosts:
                self._send(403, {"error": "bad Host"})
                return False
            if need_token and not hmac.compare_digest(
                    (self.headers.get(TOKEN_HEADER) or "").encode(), token.encode()):
                self._send(401, {"error": "missing or wrong %s" % TOKEN_HEADER})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard(need_token=False):
                return
            if self.path.split("?")[0] == "/health":
                self._send(200, hub.health())
            else:
                self._send(405, {"error": "POST /mcp; no SSE stream is offered"})

        def do_DELETE(self) -> None:  # noqa: N802
            if self._guard(need_token=True):
                self._send(200, None)

        def do_POST(self) -> None:  # noqa: N802
            # Drain the body FIRST: answering a refused request with its body
            # unread makes Windows reset the socket instead of delivering the 401.
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if 0 < n <= 16 * 1024 * 1024 else b""
            except (ValueError, OSError):
                raw = b""
            if not self._guard(need_token=True):
                return
            if self.path.split("?")[0] != "/mcp":
                self._send(404, {"error": "not found"})
                return
            try:
                msg = json.loads(raw or b"null")
            except ValueError:
                self._send(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
                return
            if not isinstance(msg, dict):
                self._send(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "one JSON object"}})
                return
            project_dir = norm_dir(self.headers.get(PROJECT_HEADER), hub.default_dir)
            resp = hub.handle(msg, project_dir)
            extra = {"Mcp-Session-Id": "aw-hub"} if msg.get("method") == "initialize" else {}
            if resp is None:
                self._send(202, None, extra)
            else:
                self._send(200, resp, extra)

    return Handler


def serve(port: int, config_path: Optional[Path], state_dir: Path,
          idle_ttl: float) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    token = read_or_create_token(state_dir)
    hub = Hub(brick_specs(load_config(config_path)), state_dir, idle_ttl=idle_ttl)
    try:
        httpd = ThreadingHTTPServer((HOST, port), make_handler(hub, token, port))
    except OSError as exc:
        # The port is the machine-wide mutex: a second hub exits quietly.
        up = health(port) is not None
        log("port %d busy (%s) -- %s; exiting" % (
            port, exc, "another hub is serving" if up else "held by something else"))
        write_receipt(state_dir, 0 if up else 1, "already-serving" if up else "port-taken")
        return 0 if up else 1
    httpd.daemon_threads = True
    write_receipt(state_dir, 0, "serving")
    (state_dir / "hub.pid").write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=hub.reaper_loop, daemon=True, name="reaper").start()
    log("serving %d bricks on http://%s:%d/mcp (pid %d)"
        % (len(hub.order), HOST, port, os.getpid()))
    try:
        httpd.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        log("interrupted; stopping")
    finally:
        hub.shutdown()
        httpd.server_close()
    return 0


def write_receipt(state_dir: Path, exit_code: int, state: str) -> None:
    """The verdict an awrise --detach wake is judged on (WL006 reads exit_code)."""
    try:
        (state_dir / "last_start.json").write_text(json.dumps({
            "exit_code": exit_code, "state": state, "pid": os.getpid(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}), encoding="utf-8")
    except OSError as exc:
        log("receipt write failed: %s" % exc)


def health(port: int, timeout: float = 2.0) -> Optional[dict]:
    req = urllib.request.Request("http://%s:%d/health" % (HOST, port))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- loopback
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ── durable keep-alive: a per-user logon task (Windows) ─────────────────────


def _pythonw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.is_file() else exe)


def install_task(port: int) -> int:
    if os.name != "nt":
        print("install: Windows logon task only; elsewhere run `awnode hub serve` "
              "from systemd --user / launchd")
        return 2
    tr = '"%s" "%s" serve --port %d' % (_pythonw(), Path(__file__).resolve(), port)
    cmd = ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
           "/TN", TASK_NAME, "/TR", tr]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    print((out.stdout or out.stderr).strip())
    if out.returncode != 0:
        return 1
    run = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    print((run.stdout or run.stderr).strip())
    return 0 if run.returncode == 0 else 1


def uninstall_task() -> int:
    if os.name != "nt":
        return 2
    out = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((out.stdout or out.stderr).strip())
    return 0 if out.returncode == 0 else 1


# ── self-test ──────────────────────────────────────────────────────────────


class _FakeChild:
    """In-memory brick: no process, so the routing contract is measured."""

    starts: List[Tuple[str, str]] = []

    def __init__(self, name: str, argv: List[str], cwd: str, env: Dict[str, str],
                 log_dir: Path):
        self.name, self.cwd, self.env = name, cwd, env
        self.last_used = time.monotonic()
        self.tools = [{"name": "%s_do" % name, "inputSchema": {"type": "object"}},
                      {"name": "shared", "inputSchema": {"type": "object"}}]
        self.calls: List[dict] = []
        self.on_tools_changed = None
        self._alive = True

    def start(self) -> None:
        _FakeChild.starts.append((self.name, self.cwd))

    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self._alive = False

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 1.0) -> dict:
        self.calls.append({"method": method, "params": params})
        return {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "%s:%s:%s" % (
                self.name, (params or {}).get("name"), self.env.get("CLAUDE_PROJECT_DIR"))}]}}


def _self_test() -> int:
    import tempfile

    failures: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "state"
        d1 = Path(td) / "repo1"
        d2 = Path(td) / "repo2"
        d1.mkdir()
        d2.mkdir()
        n1, n2 = norm_dir(str(d1), ""), norm_dir(str(d2), "")
        specs = [{"name": "alpha", "argv": ["x"]}, {"name": "beta", "argv": ["x"]},
                 {"name": "ghost", "module": "no_such_module_aw_hub_selftest"}]
        _FakeChild.starts = []
        hub = Hub(specs, state, default_dir=str(d1), idle_ttl=10.0, max_dirs=2,
                  child_factory=_FakeChild)

        # A. tools/list learns every installed brick; a collision is renamed,
        # the first brick keeps the bare name; a missing brick is an error, not a crash.
        resp = hub.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, n1) or {}
        names = sorted(t["name"] for t in (resp.get("result") or {}).get("tools", []))
        want = sorted(["alpha_do", "shared", "beta_do", "beta__shared"])
        if names != want:
            failures.append("A: tools/list %s != %s" % (names, want))
        if "ghost" not in hub.errors:
            failures.append("A: a missing brick was not recorded as an error: %s" % hub.errors)

        # B. calls route by name, rewrite a renamed tool back, carry the project dir.
        r = hub.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "beta__shared", "arguments": {}}}, n2) or {}
        text = ((r.get("result") or {}).get("content") or [{}])[0].get("text", "")
        if text != "beta:shared:%s" % n2:
            failures.append("B: routed call answered %r" % text)
        if ("beta", n2) not in _FakeChild.starts:
            failures.append("B: no child started for (beta, repo2): %s" % _FakeChild.starts)

        # C. same (brick, dir) reuses its child: no second start.
        before = len(_FakeChild.starts)
        hub.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "beta_do"}}, n2)
        if len(_FakeChild.starts) != before:
            failures.append("C: a live child was restarted")

        # D. unknown tool -> -32602, never a crash.
        r = hub.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "nope"}}, n1) or {}
        if (r.get("error") or {}).get("code") != -32602:
            failures.append("D: unknown tool answered %s" % r)

        # E. idle reaping stops children; notifications get no response.
        reaped = hub.reap_idle(now=time.monotonic() + 3600)
        if reaped < 1 or hub.children:
            failures.append("E: reap_idle reaped %d, left %d" % (reaped, len(hub.children)))
        if hub.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, n1) is not None:
            failures.append("E: a notification got a response")

        # F. the tool cache persists: a new hub serves the list with no child.
        hub2 = Hub(specs, state, default_dir=str(d1), child_factory=_FakeChild)
        if len(hub2.routes()[0]) != 4:
            failures.append("F: tool cache not reloaded: %s" % hub2.routes()[0])

        # G. project dir normalisation refuses a non-directory.
        if norm_dir(str(Path(td) / "missing"), "FALLBACK") != "FALLBACK":
            failures.append("G: a missing project dir was accepted")

        # H. brick_argv: main -> python -c with the console-script argv; absent -> None.
        argv = brick_argv({"name": "awm", "main": "json:dumps", "args": ["mcp"]}, "PY")
        if not argv or argv[:2] != ["PY", "-c"] or "['awm', 'mcp']" not in argv[2]:
            failures.append("H: brick_argv main form wrong: %s" % argv)
        if brick_argv({"name": "zz-no-such-brick-zz", "module": "zz_no_such_mod_zz"}) is not None:
            failures.append("H: an absent brick resolved to an argv")

        # I. HTTP guard: token required, Origin refused, health open.
        failures.extend(_self_test_http(hub, state))

    if failures:
        print("SELF-TEST FAILED")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("SELF-TEST PASSED -- tools/list merge + collision rename, missing brick recorded, "
          "call routing + name rewrite + project dir, child reuse, unknown tool, idle reap, "
          "tool cache persistence, dir normalisation, brick argv, HTTP token/Origin guard")
    return 0


def _self_test_http(hub: Hub, state: Path) -> List[str]:
    failures: List[str] = []
    token = read_or_create_token(state)
    try:
        httpd = ThreadingHTTPServer((HOST, 0), BaseHTTPRequestHandler)
    except OSError as exc:
        print("CANNOT RUN: no loopback socket (%s)" % exc)
        raise SystemExit(2) from exc
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(hub, token, port)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    url = "http://%s:%d" % (HOST, port)

    def post(headers: Dict[str, str]) -> int:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        req = urllib.request.Request(url + "/mcp", data=body, headers={
            "Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 -- loopback
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        if post({}) != 401:
            failures.append("I: a request without the token was not refused")
        if post({TOKEN_HEADER: token, "Origin": "http://evil.example"}) != 403:
            failures.append("I: a browser Origin was not refused")
        if post({TOKEN_HEADER: token}) != 200:
            failures.append("I: a tokened request was refused")
        with urllib.request.urlopen(url + "/health", timeout=5) as resp:  # noqa: S310
            if not json.loads(resp.read()).get("ok"):
                failures.append("I: /health not ok")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return failures


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="awnode hub", description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", default="serve",
                    choices=["serve", "status", "install", "uninstall"])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--config", default=None,
                    help="brick overrides JSON (default: <state dir>/config.json)")
    ap.add_argument("--idle-ttl", type=float, default=900.0,
                    help="seconds an unused brick child lives (default 900)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        try:
            return _self_test()
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 -- a crashed self-test cannot judge
            print("SELF-TEST COULD NOT RUN: %s: %s" % (type(exc).__name__, exc))
            return 2
    if args.command == "status":
        h = health(args.port)
        print(json.dumps(h, indent=2) if h else "aw-hub is not answering on %d" % args.port)
        return 0 if h else 1
    if args.command == "install":
        return install_task(args.port)
    if args.command == "uninstall":
        return uninstall_task()
    config = Path(args.config) if args.config else STATE_DIR / "config.json"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(STATE_DIR / "hub.log", "a", encoding="utf-8",  # noqa: SIM115
                          buffering=1)
    except OSError as exc:
        log("hub.log unwritable (%s); logging to the inherited stderr" % exc)
    return serve(args.port, config, STATE_DIR, args.idle_ttl)


if __name__ == "__main__":
    sys.exit(main())
