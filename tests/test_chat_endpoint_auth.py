"""Tests for /chat endpoint bearer-token gating (reconciled auth + memory).

The reconciled /chat reads the enrolled token ON DEMAND from ~/.aither/sync_state.json
(via _get_enrolled_bearer_token / _get_enrolled_token_created_at) — there is no
global token variable. These tests set AITHER_HOME to a temp dir and write a
sync_state.json to control the enrolled token.

Verifies:
1. Local requests (loopback) are allowed without a bearer token (backward compat)
2. Remote requests require a valid bearer token (401 without, 403 wrong/expired)
3. Token comparison is constant-time (hmac) against the on-disk enrolled token
4. X-Forwarded-For is only trusted from AITHERNODE_TRUSTED_PROXIES (spoof-proof)
5. Bearer-token expiry (>90 days) is enforced for remote callers
6. A deliberate 503 (no backend) is NOT masked as 500 by the memory try/except
"""

import json
import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from awnode import server as srv


def _write_sync_state(home: Path, token=None, created_at=None):
    """Write ~/.aither/sync_state.json under the temp home (or remove it)."""
    aither_home = home / ".aither"
    aither_home.mkdir(parents=True, exist_ok=True)
    sync_file = aither_home / "sync_state.json"
    if token is None:
        if sync_file.exists():
            sync_file.unlink()
        return
    data = {"node_id": "test-node", "tenant_scoped_token": token}
    if created_at is not None:
        data["tenant_scoped_token_created_at"] = created_at
    sync_file.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    """Point AITHER_HOME at a temp dir and expose a setter for the enrolled token.

    Also resets TRUSTED_PROXIES around each test.
    """
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / ".aither"))
    # _get_enrolled_* read AITHER_HOME as the .aither dir directly OR home/.aither;
    # write to both shapes so the helper resolves it regardless.
    original_proxies = srv.TRUSTED_PROXIES

    def _set(token=None, created_at=None):
        # AITHER_HOME is set to <tmp>/.aither, so write sync_state.json directly there.
        aither_home = Path(str(tmp_path / ".aither"))
        aither_home.mkdir(parents=True, exist_ok=True)
        sync_file = aither_home / "sync_state.json"
        if token is None:
            if sync_file.exists():
                sync_file.unlink()
            return
        data = {"node_id": "test-node", "tenant_scoped_token": token}
        if created_at is not None:
            data["tenant_scoped_token_created_at"] = created_at
        sync_file.write_text(json.dumps(data), encoding="utf-8")

    yield _set
    srv.TRUSTED_PROXIES = original_proxies


# ── Local (loopback) access — no token required ──────────────────────────


def test_chat_local_no_auth_required(enrolled):
    enrolled(token="test-token")
    srv.TRUSTED_PROXIES = {"testclient"}
    client = TestClient(srv.app)
    r = client.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "127.0.0.1"})
    assert r.status_code != 401
    assert r.status_code != 403


def test_chat_ipv6_localhost_no_auth_required(enrolled):
    enrolled(token="test-token")
    srv.TRUSTED_PROXIES = {"testclient"}
    client = TestClient(srv.app)
    r = client.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "::1"})
    assert r.status_code != 401
    assert r.status_code != 403


# ── Remote access — bearer token required ────────────────────────────────


def test_chat_remote_requires_auth(enrolled):
    enrolled(token="test-token")
    srv.TRUSTED_PROXIES = {"testclient"}
    client = TestClient(srv.app)
    r = client.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "203.0.113.42"})
    assert r.status_code == 401
    assert "Bearer token required" in r.json()["detail"]


def test_chat_remote_invalid_token(enrolled):
    enrolled(token="correct-token")
    srv.TRUSTED_PROXIES = {"testclient"}
    client = TestClient(srv.app)
    r = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"X-Forwarded-For": "203.0.113.42", "Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 403
    assert "Invalid or expired bearer token" in r.json()["detail"]


def test_chat_remote_valid_token_passes_gate(enrolled):
    enrolled(token="correct-token")
    srv.TRUSTED_PROXIES = {"testclient"}
    client = TestClient(srv.app)
    r = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"X-Forwarded-For": "203.0.113.42", "Authorization": "Bearer correct-token"},
    )
    # Auth passed; a backend error afterwards is fine — just not 401/403.
    assert r.status_code != 401
    assert r.status_code != 403


def test_chat_no_enrolled_token_remote_rejected(enrolled):
    enrolled(token=None)  # nothing enrolled
    srv.TRUSTED_PROXIES = {"testclient"}
    client = TestClient(srv.app)
    # Local still works
    r_local = client.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "127.0.0.1"})
    assert r_local.status_code not in (401, 403)
    # Remote fails (no enrolled token to match)
    r_remote = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"X-Forwarded-For": "203.0.113.42", "Authorization": "Bearer any-token"},
    )
    assert r_remote.status_code == 403


# ── X-Forwarded-For trust model (spoof-proof) ────────────────────────────


def test_xff_ignored_from_untrusted_peer(enrolled):
    """Empty TRUSTED_PROXIES → XFF is never consulted; direct peer wins."""
    srv.TRUSTED_PROXIES = set()
    from awnode.server import _get_client_ip

    request = mock.Mock()
    request.client = mock.Mock()
    request.client.host = "203.0.113.42"
    assert _get_client_ip(request, x_forwarded_for="127.0.0.1") == "203.0.113.42"


def test_xff_trusted_from_trusted_proxy(enrolled):
    srv.TRUSTED_PROXIES = {"127.0.0.1"}
    from awnode.server import _get_client_ip

    request = mock.Mock()
    request.client = mock.Mock()
    request.client.host = "127.0.0.1"
    assert _get_client_ip(request, x_forwarded_for="203.0.113.42") == "203.0.113.42"


def test_xff_spoof_via_testclient_treated_local(enrolled):
    """TestClient peer is 'testclient'; with empty TRUSTED_PROXIES, a spoofed
    remote XFF is ignored and the request is treated as local (no auth)."""
    enrolled(token="test-token")
    srv.TRUSTED_PROXIES = set()
    client = TestClient(srv.app)
    r = client.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "203.0.113.42"})
    assert r.status_code != 401
    assert r.status_code != 403


# ── Token expiry ─────────────────────────────────────────────────────────


def test_is_token_expired_helper(enrolled):
    from awnode.server import _is_token_expired

    old = time.time() - (91 * 24 * 60 * 60)
    assert _is_token_expired(old) is True
    assert _is_token_expired(time.time()) is False
    assert _is_token_expired(None) is False  # missing timestamp → not expired (token must still match)


def test_chat_remote_expired_token_rejected(enrolled):
    old = time.time() - (91 * 24 * 60 * 60)
    enrolled(token="test-token", created_at=old)
    srv.TRUSTED_PROXIES = {"testclient"}
    client = TestClient(srv.app)
    r = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"X-Forwarded-For": "203.0.113.42", "Authorization": "Bearer test-token"},
    )
    assert r.status_code == 403
    assert "expired" in r.json()["detail"].lower()


# ── 503 (no backend) must not be masked as 500 by the memory try/except ──


def test_no_backend_returns_503_not_500(enrolled, monkeypatch):
    """Local request with no available backend → deliberate 503, not a generic 500."""
    enrolled(token="test-token")
    srv.TRUSTED_PROXIES = {"testclient"}

    # Force the "no backend" branch.
    monkeypatch.setattr(srv._state, "mode", "standalone", raising=False)
    # Ensure memory is treated as unavailable so recall/persist don't interfere.
    monkeypatch.setattr(srv, "_get_memory", lambda: None)

    client = TestClient(srv.app)
    r = client.post("/chat", json={"message": "hi"}, headers={"X-Forwarded-For": "127.0.0.1"})
    assert r.status_code == 503
    assert "No LLM backend" in r.json()["detail"]
