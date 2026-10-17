"""The operator's own OpenAI-compatible backend (`custom_backend`).

Three properties, and the third is the one that needs a test rather than a review:

 1. A configured backend that PROBES OK becomes the mode and wins the routing. A
    configured one that does not probe must NOT — awnode's own rule is that only a 200
    on `/v1/models` proves an OpenAI-compatible server, and the measured cost of the
    looser `/health` rule was media-forge on :8200 being picked as "llama.cpp" and
    404'ing every chat.
 2. The user's key is sent to the user's host and to NOTHING else. Keyed on an exact
    base match, so a turn that fell through to vLLM or Ollama cannot pick it up.
 3. The key never appears in a log record, a response body, or the startup banner.
    Asserted by capturing logging and scanning every emitted surface for the value —
    a review can miss a `%s` of a dict; this cannot.

No real credential is used anywhere in this file: the sentinel below is a fixed string
chosen so a grep for it finds only this test.
"""

import importlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

SENTINEL = "sentinel-not-a-real-key-0000"


def _stub_probe(server, ok_urls):
    """Replace the network probe with a set-membership test."""
    async def fake_probe(url, path="/health"):
        return url.rstrip("/") in {u.rstrip("/") for u in ok_urls}
    server._probe = fake_probe

    async def fake_probe_openai(url):
        return await fake_probe(url, "/v1/models")
    server._probe_openai = fake_probe_openai


@pytest.fixture()
def server(monkeypatch, tmp_path):
    """A fresh `awnode.server` with an isolated AITHER_HOME and no env seed."""
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.delenv("AITHERNODE_CUSTOM_BACKEND_URL", raising=False)
    monkeypatch.delenv("AITHERNODE_CUSTOM_BACKEND_MODEL", raising=False)
    monkeypatch.delenv("AITHERNODE_CUSTOM_BACKEND_API_KEY", raising=False)
    import awnode.sync as sync_mod
    importlib.reload(sync_mod)
    import awnode.server as server_mod
    importlib.reload(server_mod)
    # NO NETWORK, BY DEFAULT. `TestClient(app)` runs the lifespan, which calls
    # `_state.refresh()`, which probes six URLs — one of them `https://mcp.aitherium.com`.
    # Left real, this suite took 140 s and its result depended on whether the box had
    # internet. A test that is slow AND environment-dependent gets marked skip rather
    # than fixed, so the default here is "nothing answers" and each test opts in.
    _stub_probe(server_mod, [])
    yield server_mod
    server_mod._CUSTOM.update({"base_url": "", "model": "", "api_key": ""})


# ── 1. probe decides ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_ok_makes_custom_the_mode_and_the_route(server):
    server._CUSTOM.update({"base_url": "https://api.example.invalid", "model": "m1",
                           "api_key": SENTINEL})
    _stub_probe(server, ["https://api.example.invalid", server.OLLAMA_URL])
    await server._state.refresh()

    assert server._state.custom is True
    assert server._state.mode == "custom"
    assert server._openai_backend_for("anything") == "https://api.example.invalid"
    # ...including a model name it does not serve: the id is rewritten, never 404'd.
    assert await server._resolve_backend_model(
        "https://api.example.invalid", "bonsai-27b") == "m1"


@pytest.mark.asyncio
async def test_probe_fails_so_the_ladder_falls_to_the_next_rung(server):
    server._CUSTOM.update({"base_url": "https://api.example.invalid", "model": "m1",
                           "api_key": SENTINEL})
    # The custom URL is configured and does NOT answer /v1/models; Ollama does.
    _stub_probe(server, [server.OLLAMA_URL])
    await server._state.refresh()

    assert server._state.custom is False
    assert server._state.mode == "ollama"
    assert server._openai_backend_for("anything") == server.OLLAMA_URL


@pytest.mark.asyncio
async def test_no_custom_configured_changes_nothing(server):
    _stub_probe(server, [server.OLLAMA_URL])
    await server._state.refresh()
    assert server._state.custom is False
    assert server._state.mode == "ollama"


# ── 2. the key goes to exactly one host ─────────────────────────────────────

def test_auth_header_only_for_the_configured_base(server):
    server._CUSTOM.update({"base_url": "https://api.example.invalid", "model": "",
                           "api_key": SENTINEL})
    assert server._custom_auth_headers("https://api.example.invalid") == {
        "Authorization": f"Bearer {SENTINEL}"
    }
    # trailing-slash variants are the same host
    assert server._custom_auth_headers("https://api.example.invalid/")["Authorization"]
    # every other backend gets NOTHING
    for other in (server.OLLAMA_URL, server.VLLM_URL, server.BONSAI_URL,
                  "https://api.example.invalid.evil", ""):
        assert server._custom_auth_headers(other) == {}


def test_no_key_configured_means_no_header(server):
    server._CUSTOM.update({"base_url": "https://api.example.invalid", "model": "",
                           "api_key": ""})
    assert server._custom_auth_headers("https://api.example.invalid") == {}


# ── 3. origin guard ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,ok", [
    ("https://api.openai.com", True),
    ("https://llm.example.invalid:8443/v1", True),
    ("http://127.0.0.1:1234", True),
    ("http://127.5.0.9:1234", True),
    ("http://localhost:8080/v1", True),
    ("http://[::1]:8080", True),
    ("http://192.168.1.5:8000", False),
    ("http://10.0.0.4/v1", False),
    ("http://api.openai.com", False),
    ("file:///models", False),
    ("", False),
])
def test_is_safe_custom_base(server, url, ok):
    assert server._is_safe_custom_base(url) is ok


def test_put_config_refuses_a_plain_http_lan_backend(server):
    with TestClient(server.app) as client:
        r = client.put("/config", json={"custom_backend": {
            "base_url": "http://192.168.1.5:8000", "api_key": SENTINEL}})
    assert r.status_code == 400
    assert "unencrypted" in r.text
    # And it was refused BEFORE storage: nothing was kept.
    assert server._CUSTOM["api_key"] == ""


# ── 4. the key is never echoed, logged, or persisted world-readably ─────────

def test_put_config_never_echoes_the_key(server, caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(server.app) as client:
        r = client.put("/config", json={"custom_backend": {
            "base_url": "https://api.example.invalid",
            "model": "m1",
            "api_key": SENTINEL,
        }})
        assert r.status_code == 200, r.text
        assert SENTINEL not in r.text
        summary = r.json()["config"]["custom_backend"]
        assert summary == {"base_url": "https://api.example.invalid",
                           "model": "m1", "has_api_key": True}

        got = client.get("/config")
        assert SENTINEL not in got.text
        assert got.json()["workspace"]["custom_backend"]["has_api_key"] is True

    # NOT ONE log record may carry it — including the "custom backend updated" line.
    for rec in caplog.records:
        assert SENTINEL not in rec.getMessage()


def test_a_model_only_update_does_not_wipe_the_key(server):
    with TestClient(server.app) as client:
        client.put("/config", json={"custom_backend": {
            "base_url": "https://api.example.invalid", "api_key": SENTINEL}})
        assert server._CUSTOM["api_key"] == SENTINEL
        # `api_key: None` means "leave it alone" — a settings form saving the model
        # must not silently log you out of your own backend.
        client.put("/config", json={"custom_backend": {
            "base_url": "https://api.example.invalid", "model": "m2"}})
        assert server._CUSTOM["api_key"] == SENTINEL
        assert server._CUSTOM["model"] == "m2"


def test_clearing_the_url_forgets_the_key(server):
    with TestClient(server.app) as client:
        client.put("/config", json={"custom_backend": {
            "base_url": "https://api.example.invalid", "api_key": SENTINEL}})
        client.put("/config", json={"custom_backend": {"base_url": ""}})
    assert server._CUSTOM == {"base_url": "", "model": "", "api_key": ""}


def test_state_file_holds_the_key_only_at_mode_0600(server, tmp_path):
    import os
    import stat
    with TestClient(server.app) as client:
        client.put("/config", json={"custom_backend": {
            "base_url": "https://api.example.invalid", "api_key": SENTINEL}})

    state = tmp_path / "sync_state.json"
    assert state.exists()
    data = json.loads(state.read_text())
    assert data["custom_backend"]["api_key"] == SENTINEL
    if os.name != "nt":
        # The same file has ALWAYS held `tenant_scoped_token`; it was world-readable
        # until this change. Windows ignores POSIX modes, so the assertion is scoped.
        assert stat.S_IMODE(state.stat().st_mode) == 0o600


def test_a_restart_restores_the_backend_from_disk(server, monkeypatch, tmp_path):
    with TestClient(server.app) as client:
        client.put("/config", json={"custom_backend": {
            "base_url": "https://api.example.invalid", "model": "m1",
            "api_key": SENTINEL}})

    server._CUSTOM.update({"base_url": "", "model": "", "api_key": ""})
    server._seed_custom_from_disk()
    assert server._CUSTOM["base_url"] == "https://api.example.invalid"
    assert server._CUSTOM["model"] == "m1"
    assert server._CUSTOM["api_key"] == SENTINEL
