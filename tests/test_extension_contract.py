"""The awconnect extension's side of cross-surface contract C1, and the node tool scope rule.

C1: the Living OS overlay's service worker calls this node with
``Origin: chrome-extension://<id>`` on GET /health, GET /v1/models,
POST /v1/chat/completions and POST /mcp. Only the two PUBLISHED awconnect ids plus
AWNODE_EXTENSION_IDS are admitted there; another installed extension is refused, and
every route outside C1 still refuses an extension origin.

Node tool scope rule: anything browser-callable exposes read-only filesystem + web
search only. POST /mcp tools/call is asserted against the filesystem, not against a
status code alone: a refused write_file must leave NO file behind.
"""

import importlib
import json

import pytest
from fastapi.testclient import TestClient

STORE_ID = "hlmfknhcfhjjngckfpacgleffckpmphe"
ALT_ID = "peeojgjhjficedkncdejbfnacooodbak"
STORE = f"chrome-extension://{STORE_ID}"
ALT = f"chrome-extension://{ALT_ID}"
OTHER = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


@pytest.fixture()
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AWNODE_EXTENSION_IDS", raising=False)
    monkeypatch.delenv("AWNODE_BROWSER_FULL_TOOLS", raising=False)
    srv = importlib.import_module("awnode.server")

    # No network: nothing answers, so /v1/models is an empty list and chat is a 503.
    async def _nothing(url, path="/health"):
        return False

    async def _nothing_openai(url):
        return False

    monkeypatch.setattr(srv, "_probe", _nothing)
    monkeypatch.setattr(srv, "_probe_openai", _nothing_openai)
    for flag in ("genesis", "vllm", "llamacpp", "bonsai", "lmstudio", "ollama", "cloud", "custom"):
        monkeypatch.setattr(srv._state, flag, False)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "hello.txt").write_text("hi from the shared folder\n", encoding="utf-8")
    monkeypatch.setattr(srv, "AITHERNODE_FS_ROOT", shared)
    return srv


def _rpc(client, method, params=None, headers=None):
    body = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers or {})


# ── C1: origins ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin", [STORE, ALT])
def test_pinned_extension_is_served_on_every_c1_route(server, origin):
    c = TestClient(server.app)
    h = {"Origin": origin}
    health = c.get("/health", headers=h)
    assert health.status_code == 200
    assert health.headers.get("access-control-allow-origin") == origin
    models = c.get("/v1/models", headers=h)
    assert models.status_code == 200 and models.json()["object"] == "list"
    # Admitted past the Origin gate: with no backend it is the 503, never the 403.
    chat = c.post("/v1/chat/completions", json={"model": "auto", "messages": []}, headers=h)
    assert chat.status_code == 503
    listed = _rpc(c, "tools/list", headers=h)
    assert listed.status_code == 200 and "tools" in listed.json()["result"]


@pytest.mark.parametrize("origin", [STORE, ALT])
def test_cors_preflight_answers_the_pinned_extension(server, origin):
    c = TestClient(server.app)
    r = c.options(
        "/v1/chat/completions",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type"},
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


def test_cors_preflight_ignores_another_extension(server):
    c = TestClient(server.app)
    r = c.options(
        "/v1/chat/completions",
        headers={"Origin": OTHER, "Access-Control-Request-Method": "POST"},
    )
    assert r.headers.get("access-control-allow-origin") != OTHER


def test_an_unpinned_extension_is_refused_on_the_spending_routes(server):
    c = TestClient(server.app)
    h = {"Origin": OTHER}
    assert c.get("/v1/models", headers=h).status_code == 403
    assert c.post("/v1/chat/completions", json={"messages": []}, headers=h).status_code == 403
    assert _rpc(c, "tools/list", headers=h).status_code == 403
    assert _rpc(c, "tools/call", {"name": "read_file", "arguments": {"path": "hello.txt"}},
                headers=h).status_code == 403
    # ...while the discovery-only GET /mcp/tools keeps its documented any-id behaviour.
    assert c.get("/mcp/tools", headers=h).status_code == 200


def test_env_ids_are_admitted_alongside_the_built_in_pins(server, monkeypatch):
    monkeypatch.setenv("AWNODE_EXTENSION_IDS", "abcdefghijklmnopabcdefghijklmnop")
    c = TestClient(server.app)
    assert c.get("/v1/models", headers={"Origin": OTHER}).status_code == 200
    assert c.get("/v1/models", headers={"Origin": STORE}).status_code == 200
    stranger = "chrome-extension://pppppppppppppppppppppppppppppppp"
    assert c.get("/v1/models", headers={"Origin": stranger}).status_code == 403


def test_pinned_extension_is_still_refused_outside_c1(server):
    # PUT /config writes the owner's backend + key: not a C1 route.
    c = TestClient(server.app)
    r = c.put("/config", json={"custom_backend": {"base_url": ""}}, headers={"Origin": STORE})
    assert r.status_code == 403
    assert c.get("/packs/installed", headers={"Origin": STORE}).status_code == 403


def test_pinned_extension_from_a_remote_peer_still_needs_the_bearer(server):
    # Admitting the origin is not admitting the caller: loopback-or-bearer is unchanged.
    remote = TestClient(server.app, client=("10.0.0.5", 50000))
    assert remote.get("/v1/models", headers={"Origin": STORE}).status_code == 403
    assert _rpc(remote, "tools/call", {"name": "read_file", "arguments": {"path": "hello.txt"}},
                headers={"Origin": STORE}).status_code == 403


def test_foreign_web_origin_is_refused_on_post_mcp(server):
    c = TestClient(server.app)
    assert _rpc(c, "tools/list", headers={"Origin": "https://evil.example"}).status_code == 403


# ── POST /mcp tools/call + the node tool scope rule ───────────────────────────

def test_extension_sees_only_the_read_only_tool_set(server):
    c = TestClient(server.app)
    names = {t["name"] for t in _rpc(c, "tools/list", headers={"Origin": STORE}).json()["result"]["tools"]}
    assert names == {"read_file", "list_directory", "web_search"}


def test_extension_reads_inside_the_shared_folder(server):
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "read_file", "arguments": {"path": "hello.txt"}},
             headers={"Origin": STORE}).json()
    assert r["result"]["isError"] is False
    assert "hi from the shared folder" in json.loads(r["result"]["content"][0]["text"])["content"]
    listed = _rpc(c, "tools/call", {"name": "list_dir", "arguments": {}}, headers={"Origin": STORE}).json()
    assert "hello.txt" in listed["result"]["content"][0]["text"]


def test_extension_read_is_jailed_to_the_shared_folder(server, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("owner-only", encoding="utf-8")
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "read_file", "arguments": {"path": str(secret)}},
             headers={"Origin": STORE}).json()
    assert r["result"]["isError"] is True
    assert "owner-only" not in r["result"]["content"][0]["text"]


@pytest.mark.parametrize("origin", [STORE, "https://aitherium.com", "http://localhost:3000"])
def test_browser_origin_cannot_write_or_run(server, tmp_path, origin):
    target = tmp_path / "planted.txt"
    c = TestClient(server.app)
    w = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "x"}},
             headers={"Origin": origin}).json()
    assert w["error"]["code"] == -32601 and "read-only" in w["error"]["message"]
    assert not target.exists()
    s = _rpc(c, "tools/call", {"name": "run_command", "arguments": {"command": "echo pwned"}},
             headers={"Origin": origin}).json()
    assert s["error"]["code"] == -32601


def test_sec_fetch_site_alone_marks_a_browser_caller(server, tmp_path):
    target = tmp_path / "planted.txt"
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "x"}},
             headers={"Sec-Fetch-Site": "same-origin"}).json()
    assert "error" in r and not target.exists()


def test_owner_opt_in_lifts_the_restriction(server, tmp_path, monkeypatch):
    monkeypatch.setenv("AWNODE_BROWSER_FULL_TOOLS", "1")
    target = tmp_path / "allowed.txt"
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "ok"}},
             headers={"Origin": STORE}).json()
    assert r["result"]["isError"] is False
    assert target.read_text(encoding="utf-8") == "ok"


OWNER_TOKEN = "enrolled-owner-token-0123456789"


@pytest.fixture()
def enrolled(server, monkeypatch):
    monkeypatch.setattr(server, "_get_enrolled_bearer_token", lambda: OWNER_TOKEN)
    monkeypatch.delenv("AITHER_MCP_KEY", raising=False)
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


def test_unauthenticated_loopback_caller_gets_only_the_read_only_set(server, enrolled, tmp_path):
    # Any process that can reach 127.0.0.1 (another OS user, a --network=host
    # container) must not get a shell over plain HTTP: no credential, no write/run.
    target = tmp_path / "cli.txt"
    c = TestClient(server.app)
    names = {t["name"] for t in _rpc(c, "tools/list").json()["result"]["tools"]}
    assert names == {"read_file", "list_directory", "web_search"}
    w = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "x"}}).json()
    assert w["error"]["code"] == -32601 and "awnode mcp" in w["error"]["message"]
    script = f"open(r'{target}','w').write('pwned')"
    r = _rpc(c, "tools/call", {"name": "run_command", "arguments": {"command": f'python -c "{script}"'}}).json()
    assert r["error"]["code"] == -32601
    assert not target.exists()
    wrong = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "x"}},
                 headers={"Authorization": "Bearer not-the-token"}).json()
    assert wrong["error"]["code"] == -32601 and not target.exists()


def test_loopback_cli_caller_with_the_owner_token_gets_the_full_registry(server, enrolled, tmp_path):
    target = tmp_path / "cli.txt"
    c = TestClient(server.app)
    names = {t["name"] for t in _rpc(c, "tools/list", headers=enrolled).json()["result"]["tools"]}
    assert "write_file" in names and "run_command" in names
    r = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "cli"}},
             headers=enrolled).json()
    assert r["result"]["isError"] is False
    assert target.read_text(encoding="utf-8") == "cli"


def test_aither_mcp_key_is_an_owner_credential(server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_get_enrolled_bearer_token", lambda: None)
    monkeypatch.setenv("AITHER_MCP_KEY", "mcp-key-abcdef")
    target = tmp_path / "key.txt"
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "k"}},
             headers={"Authorization": "Bearer mcp-key-abcdef"}).json()
    assert r["result"]["isError"] is False and target.exists()


def test_owner_token_never_unlocks_shell_for_a_browser_origin(server, enrolled, tmp_path):
    target = tmp_path / "planted.txt"
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "x"}},
             headers={**enrolled, "Origin": STORE}).json()
    assert r["error"]["code"] == -32601 and not target.exists()


@pytest.mark.parametrize("proxy_header", [
    {"X-Forwarded-For": "203.0.113.9"},
    {"Cf-Connecting-Ip": "203.0.113.9"},
    {"Forwarded": "for=203.0.113.9"},
    {"X-Real-Ip": "203.0.113.9"},
    {"X-Forwarded-Host": "node.example.com"},
])
@pytest.mark.parametrize("opt_in", [False, True])
def test_tunnelled_request_from_a_loopback_peer_cannot_write(server, enrolled, monkeypatch, tmp_path,
                                                             proxy_header, opt_in):
    # A same-host tunnel/reverse proxy reaches the node as 127.0.0.1 (TRUSTED_PROXIES is
    # empty by default). Its proxy headers mark the request remote: even with the owner
    # token and even with the opt-in, write_file is refused and no file appears.
    if opt_in:
        monkeypatch.setenv("AWNODE_BROWSER_FULL_TOOLS", "1")
    target = tmp_path / "tunnel.txt"
    c = TestClient(server.app, client=("127.0.0.1", 50000))
    r = _rpc(c, "tools/call", {"name": "write_file", "arguments": {"path": str(target), "content": "x"}},
             headers={**enrolled, **proxy_header, "Host": "node.example.com"}).json()
    assert r["error"]["code"] == -32601
    assert not target.exists()


# ── list_directory pattern jail ───────────────────────────────────────────────

@pytest.mark.parametrize("pattern,recursive", [
    ("../*", False),
    ("../../outside/*", False),
    ("..\\*", False),
    ("../**/*", True),
    ("sub/../../*", False),
])
def test_list_directory_pattern_cannot_escape_the_shared_folder(server, tmp_path, pattern, recursive):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("owner-only", encoding="utf-8")
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "list_directory", "arguments": {
        "path": ".", "pattern": pattern, "recursive": recursive}}, headers={"Origin": STORE}).json()
    text = r["result"]["content"][0]["text"]
    assert r["result"]["isError"] is True
    assert "secret.txt" not in text and "outside" not in text


def test_list_directory_absolute_pattern_is_refused(server, tmp_path):
    c = TestClient(server.app)
    for pattern in (str(tmp_path / "*"), "/etc/*", "C:/Users/*"):
        r = _rpc(c, "tools/call", {"name": "list_directory", "arguments": {"pattern": pattern}},
                 headers={"Origin": STORE}).json()
        assert r["result"]["isError"] is True, pattern


def test_list_directory_drops_symlinks_out_of_the_shared_folder(server, tmp_path):
    import os
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("owner-only", encoding="utf-8")
    link = server.AITHERNODE_FS_ROOT / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")
    c = TestClient(server.app)
    r = _rpc(c, "tools/call", {"name": "list_directory", "arguments": {
        "path": ".", "pattern": "**/*", "recursive": True}}, headers={"Origin": STORE}).json()
    text = r["result"]["content"][0]["text"]
    assert r["result"]["isError"] is False
    assert "hello.txt" in text and "secret.txt" not in text


def test_unknown_tool_and_bad_params(server):
    c = TestClient(server.app)
    assert _rpc(c, "tools/call", {"name": "no_such_tool"}).json()["error"]["code"] == -32601
    assert _rpc(c, "tools/call", {"arguments": {}}).json()["error"]["code"] == -32602
    assert _rpc(c, "tools/call", {"name": "read_file", "arguments": "x"}).json()["error"]["code"] == -32602


# ── LM Studio probe ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lmstudio_is_probed_and_routed(server, monkeypatch):
    async def probe(url, path="/health"):
        return url.rstrip("/") == server.LMSTUDIO_URL.rstrip("/") and path == "/v1/models"

    async def probe_openai(url):
        return await probe(url, "/v1/models")

    monkeypatch.setattr(server, "_probe", probe)
    monkeypatch.setattr(server, "_probe_openai", probe_openai)
    monkeypatch.setattr(server, "MODE", "auto")
    await server._state.refresh()
    assert server._state.lmstudio is True
    assert server._state.mode == "lmstudio"
    assert server._openai_backend_for("auto") == server.LMSTUDIO_URL
    assert server.LMSTUDIO_URL.endswith(":1234")


@pytest.mark.asyncio
async def test_lmstudio_down_is_not_reported(server):
    await server._state.refresh()
    assert server._state.lmstudio is False
    assert server._state.mode == "standalone"
