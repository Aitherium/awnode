"""Auth tests for the awnode MCP SSE transport (P4 hardening).

Covers three layers:
  1. _bearer_ok — the constant-time header check (pure, no deps).
  2. run_sse fail-closed guard — refuses a public bind without a key; lets a
     loopback bind (or a keyed public bind) proceed past the guard.
  3. Live HTTP — the bearer middleware rejects tokenless/wrong-token requests
     with 401 and lets /health through, exercised over a real ASGI stack.
"""

from __future__ import annotations

import asyncio

import pytest

from awnode import mcp_server as m


# ── 1. _bearer_ok branches ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "header,key,expected",
    [
        ("Bearer s3cret", "s3cret", True),      # happy path
        ("Bearer wrong", "s3cret", False),      # wrong token
        ("Bearer s3cret", "", False),           # no server key -> fail closed
        ("", "s3cret", False),                  # missing header
        ("s3cret", "s3cret", False),            # missing 'Bearer ' scheme
        ("Bearer ", "s3cret", False),           # empty token
        ("Basic s3cret", "s3cret", False),      # wrong scheme
    ],
)
def test_bearer_ok(header, key, expected):
    assert m._bearer_ok(header, key) is expected


# ── 2. run_sse fail-closed guard ───────────────────────────────────────

def test_run_sse_refuses_public_bind_without_key(monkeypatch):
    """A non-loopback bind with no AITHER_MCP_KEY must refuse to start."""
    monkeypatch.delenv("AITHER_MCP_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(m.run_sse(host="0.0.0.0", port=0))
    assert exc.value.code == 2


class _PastGuard(Exception):
    """Sentinel raised from a patched _initialize to prove the guard passed."""


def _patch_init_to_sentinel(monkeypatch):
    async def _boom():
        raise _PastGuard()
    monkeypatch.setattr(m, "_initialize", _boom)


def test_run_sse_allows_loopback_without_key(monkeypatch):
    """A loopback bind is permitted without a key (dev convenience)."""
    monkeypatch.delenv("AITHER_MCP_KEY", raising=False)
    _patch_init_to_sentinel(monkeypatch)
    with pytest.raises(_PastGuard):
        asyncio.run(m.run_sse(host="127.0.0.1", port=0))


def test_run_sse_allows_public_bind_with_key(monkeypatch):
    """A public bind is permitted once a key is set."""
    monkeypatch.setenv("AITHER_MCP_KEY", "s3cret")
    _patch_init_to_sentinel(monkeypatch)
    with pytest.raises(_PastGuard):
        asyncio.run(m.run_sse(host="0.0.0.0", port=0))


# ── 3. Live HTTP enforcement over a real ASGI stack ────────────────────

def _build_test_app(key: str):
    """Rebuild the production middleware pattern around a dummy protected route."""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    class _Bearer(BaseHTTPMiddleware):
        def __init__(self, app, key):
            super().__init__(app)
            self._key = key

        async def dispatch(self, request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            if not m._bearer_ok(request.headers.get("authorization", ""), self._key):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

    async def health(_req):
        return JSONResponse({"status": "healthy"})

    async def protected(_req):
        return JSONResponse({"ok": True})

    return Starlette(
        middleware=[Middleware(_Bearer, key=key)],
        routes=[Route("/health", health), Route("/sse", protected)],
    )


def test_live_http_auth_enforcement():
    from starlette.testclient import TestClient

    client = TestClient(_build_test_app("s3cret"))

    # /health is always open (liveness probes must not need a token)
    assert client.get("/health").status_code == 200

    # protected route: no token / wrong token -> 401
    assert client.get("/sse").status_code == 401
    assert client.get("/sse", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/sse", headers={"Authorization": "s3cret"}).status_code == 401

    # correct token -> passes the gate (200 from the dummy route)
    ok = client.get("/sse", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
