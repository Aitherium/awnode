"""Enrolled-node lifecycle: state merge, token expiry, server node_id, heartbeat.

Gap analysis 2026-10-10 rows 15, 26-29: the default sync host had no handler, the
30-day token expired silently, `_save_local_state` dropped node_id, enrolled nodes
never heartbeated, and register used a second (hardware-hash) identity.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import awnode.sync as sync


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "sync_state.json"
    monkeypatch.setattr(sync, "AITHER_HOME", tmp_path)
    monkeypatch.setattr(sync, "SYNC_STATE_FILE", f)
    return f


def test_default_sync_host_is_the_genesis_bridge_not_retired_portal():
    assert "portal.aitherium.com" not in sync.DEFAULT_SYNC_URL
    assert sync.DEFAULT_SYNC_URL.endswith("/api/bridge/genesis")


def test_save_keeps_keys_written_by_enroll(state_file):
    state_file.write_text(json.dumps({
        "tenant_scoped_token": "tok", "node_id": "srv-node-1",
        "tenant_scoped_token_expires_at": "2099-01-01T00:00:00Z"}))
    c = sync.WorkspaceSyncClient()
    c._load_local_state()
    c._save_local_state()
    data = json.loads(state_file.read_text())
    assert data["node_id"] == "srv-node-1"
    assert data["tenant_scoped_token_expires_at"] == "2099-01-01T00:00:00Z"
    assert data["tenant_scoped_token"] == "tok"


def test_token_expiry_is_detected_and_surfaced(state_file):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    state_file.write_text(json.dumps({"tenant_scoped_token": "tok",
                                      "tenant_scoped_token_expires_at": past}))
    c = sync.WorkspaceSyncClient()
    c._load_local_state()
    assert c.token_expired() is True
    assert c.get_status()["needs_reenroll"] is True


def test_unknown_expiry_is_not_expired(state_file):
    state_file.write_text(json.dumps({"tenant_scoped_token": "tok"}))
    c = sync.WorkspaceSyncClient()
    c._load_local_state()
    assert c.token_expired() is False


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Client:
    calls: list = []
    reply = (200, {})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _Client.calls.append((url, json, headers))
        return _Resp(*_Client.reply)


def _patch_client(monkeypatch, reply):
    _Client.calls = []
    _Client.reply = reply
    monkeypatch.setattr(sync.httpx, "AsyncClient", _Client)


def test_enrolled_node_heartbeats_with_its_token_and_server_node_id(state_file, monkeypatch):
    state_file.write_text(json.dumps({"tenant_scoped_token": "tok", "node_id": "srv-node-1"}))
    monkeypatch.setattr(sync, "API_KEY", "")
    _patch_client(monkeypatch, (200, {"status": "ok"}))
    c = sync.WorkspaceSyncClient()
    c._load_local_state()
    out = asyncio.run(c.heartbeat())
    assert out == {"status": "ok"}
    url, body, headers = _Client.calls[0]
    assert url.endswith("/v1/endpoints/heartbeat")
    assert headers == {"Authorization": "Bearer tok"}
    assert body["node_id"] == "srv-node-1"


def test_a_refused_credential_says_re_enroll(state_file, monkeypatch):
    state_file.write_text(json.dumps({"tenant_scoped_token": "tok"}))
    _patch_client(monkeypatch, (401, {"detail": "expired"}))
    c = sync.WorkspaceSyncClient()
    c._load_local_state()
    asyncio.run(c.heartbeat())
    assert c.auth_error == "re-enroll"
    assert c.get_status()["needs_reenroll"] is True


def test_unknown_node_heartbeat_re_registers(state_file, monkeypatch):
    state_file.write_text(json.dumps({"tenant_scoped_token": "tok", "node_id": "srv-node-1"}))
    _patch_client(monkeypatch, (200, {"status": "unknown_node"}))
    c = sync.WorkspaceSyncClient()
    c._load_local_state()
    registered = []

    async def fake_register():
        registered.append(True)
        return {}

    monkeypatch.setattr(c, "register_endpoint", fake_register)
    asyncio.run(c.heartbeat())
    assert registered == [True]
