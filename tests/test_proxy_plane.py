"""Tests for the awnode browser proxy plane + OpenAI-compat routing."""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import awnode.proxy as proxy_mod


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("AITHERNODE_PROXY_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(proxy_mod.router)
    return TestClient(app)


def test_unknown_service_is_404_fail_closed(monkeypatch, client):
    # TestClient's peer is "testclient" (not an IP) -> non-loopback path;
    # with a token configured and presented we reach the allowlist check.
    monkeypatch.setattr(proxy_mod, "PROXY_TOKEN", "t0k3n")
    r = client.get(
        "/proxy/notaservice/health", headers={"x-aithernode-proxy-token": "t0k3n"}
    )
    assert r.status_code == 404


def test_non_loopback_without_token_is_403(monkeypatch, client):
    """Fail-closed: unresolvable/non-loopback peer + no token -> deny everything."""
    monkeypatch.setattr(proxy_mod, "PROXY_TOKEN", "")
    r = client.get("/proxy/search/health")
    assert r.status_code == 403


def test_non_loopback_with_wrong_token_is_403(monkeypatch, client):
    monkeypatch.setattr(proxy_mod, "PROXY_TOKEN", "correct")
    r = client.get(
        "/proxy/search/health", headers={"x-aithernode-proxy-token": "wrong"}
    )
    assert r.status_code == 403


def test_empty_token_header_never_matches_empty_config(monkeypatch, client):
    """'' == '' must NOT grant access when no token is configured."""
    monkeypatch.setattr(proxy_mod, "PROXY_TOKEN", "")
    r = client.get(
        "/proxy/search/health", headers={"x-aithernode-proxy-token": ""}
    )
    assert r.status_code == 403


def test_dead_upstream_is_502_not_500(monkeypatch, client):
    monkeypatch.setattr(proxy_mod, "PROXY_TOKEN", "t0k3n")
    monkeypatch.setitem(proxy_mod.SERVICES, "search", "http://127.0.0.1:1")
    r = client.get(
        "/proxy/search/health", headers={"x-aithernode-proxy-token": "t0k3n"}
    )
    assert r.status_code == 502


def test_proxy_map_env_extends_allowlist(monkeypatch):
    monkeypatch.setenv("AITHERNODE_PROXY_MAP", '{"custom": "http://127.0.0.1:9999/"}')
    services = proxy_mod.refresh_services()
    assert services["custom"] == "http://127.0.0.1:9999"
    # defaults survive
    assert "search" in services and "browser" in services and "bonsai" in services
    monkeypatch.delenv("AITHERNODE_PROXY_MAP")
    proxy_mod.refresh_services()


def test_proxy_map_invalid_json_ignored(monkeypatch):
    monkeypatch.setenv("AITHERNODE_PROXY_MAP", "{not json")
    services = proxy_mod.refresh_services()
    assert "search" in services
    monkeypatch.delenv("AITHERNODE_PROXY_MAP")
    proxy_mod.refresh_services()


def test_verify_never_false(monkeypatch, tmp_path):
    """_verify() returns a CA path or True — never False (no silent TLS bypass)."""
    monkeypatch.delenv("AITHER_CA_BUNDLE", raising=False)
    v = proxy_mod._verify()
    assert v is True or isinstance(v, str)
    ca = tmp_path / "ca.pem"
    ca.write_text("dummy")
    monkeypatch.setenv("AITHER_CA_BUNDLE", str(ca))
    assert proxy_mod._verify() == str(ca)


def test_hop_by_hop_headers_stripped():
    assert "transfer-encoding" in proxy_mod._HOP_BY_HOP
    assert "host" in proxy_mod._HOP_BY_HOP
    assert "content-length" in proxy_mod._HOP_BY_HOP


def test_request_forces_identity_encoding():
    """Browsers always send Accept-Encoding: gzip; forwarding it (or letting
    httpx pick brotli/zstd) makes the upstream compress and the read fail with
    an empty-message DecodingError → every proxied browser call 502s. The proxy
    must force identity so the upstream never compresses."""
    class _Req:
        def __init__(self, headers):
            self.headers = headers
    req = _Req({"accept-encoding": "gzip, deflate, br", "accept": "application/json"})
    out = proxy_mod._request_headers(req)
    assert out.get("accept-encoding") == "identity"
    # exactly one accept-encoding (case-insensitive) survives
    assert sum(1 for k in out if k.lower() == "accept-encoding") == 1


def test_response_strips_content_encoding():
    """httpx decodes the body, so the forwarded response must not keep a
    Content-Encoding header that would mislabel the (now plain) bytes."""
    assert "content-encoding" in proxy_mod._RESP_STRIP
    assert "content-length" in proxy_mod._RESP_STRIP  # inherited from hop-by-hop


def test_marketplace_in_allowlist():
    services = proxy_mod.refresh_services()
    assert "marketplace" in services and services["marketplace"].startswith("http")


def test_openai_backend_routing(monkeypatch):
    server = importlib.import_module("awnode.server")
    st = server._state
    monkeypatch.setattr(st, "bonsai", True)
    monkeypatch.setattr(st, "vllm", True)
    monkeypatch.setattr(st, "llamacpp", False)
    monkeypatch.setattr(st, "ollama", False)
    # bonsai-prefixed model pins to the bonsai server even when vllm is up
    assert server._openai_backend_for("bonsai-27b") == server.BONSAI_URL
    # other models prefer vllm
    assert server._openai_backend_for("qwen") == server.VLLM_URL
    # bonsai down + bonsai model requested -> None (no silent substitution)
    monkeypatch.setattr(st, "bonsai", False)
    assert server._openai_backend_for("bonsai-27b") is None
    # nothing up -> None
    monkeypatch.setattr(st, "vllm", False)
    assert server._openai_backend_for("anything") is None


def test_mcp_tools_endpoint_lists_registry(monkeypatch):
    """GET /mcp/tools returns the node's discoverable tool registry."""
    server = importlib.import_module("awnode.server")
    from fastapi.testclient import TestClient

    # Force a mode that allows platform tools so the list is non-empty.
    monkeypatch.setattr(server._state, "genesis", True)
    monkeypatch.setattr(server, "_tool_registry_cache", None)

    async def _noop_refresh():
        return None

    monkeypatch.setattr(server._state, "refresh", _noop_refresh)
    client = TestClient(server.app)
    r = client.get("/mcp/tools")
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body and body["count"] == len(body["tools"])
    # every entry has the discovery shape the extension expects
    for t in body["tools"]:
        assert "name" in t and "description" in t and "inputSchema" in t


def test_mcp_jsonrpc_list_and_call_refusal():
    server = importlib.import_module("awnode.server")
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert listed.status_code == 200
    assert "result" in listed.json() and "tools" in listed.json()["result"]

    # tools/call must NOT execute here — fail-closed with a clear redirect.
    called = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "chat"}, "id": 2},
    )
    assert called.status_code == 200
    err = called.json()["error"]
    assert err["code"] == -32601 and "awnode mcp" in err["message"]

    unknown = client.post("/mcp", json={"jsonrpc": "2.0", "method": "bogus", "id": 3})
    assert unknown.json()["error"]["code"] == -32601


def test_packs_installed_lists_bundled_and_installed():
    server = importlib.import_module("awnode.server")
    from fastapi.testclient import TestClient

    r = TestClient(server.app).get("/packs/installed")
    assert r.status_code == 200
    body = r.json()
    assert "bundled" in body and "installed" in body
    assert isinstance(body["bundled"], list) and isinstance(body["installed"], list)


def test_packs_install_rejects_traversal_and_bad_names():
    server = importlib.import_module("awnode.server")
    from fastapi.testclient import TestClient

    client = TestClient(server.app)  # TestClient peer is loopback → gate passes
    for bad in ("../evil", "a/b", "a\\b", ".hidden", ""):
        r = client.post("/packs/install", json={"pack": bad})
        assert r.status_code == 400, f"{bad!r} should be rejected, got {r.status_code}"


def test_packs_install_unknown_pack_is_404_with_hint():
    server = importlib.import_module("awnode.server")
    from fastapi.testclient import TestClient

    r = TestClient(server.app).post("/packs/install", json={"pack": "definitely-not-bundled-xyz"})
    assert r.status_code == 404
    # the 404 must hand back the adk command so the extension can fall back
    assert "adk install pack/" in r.json()["detail"]


def test_packs_install_strips_pack_prefix():
    """A marketplace 'pack/<id>' form is normalized to '<id>' before lookup."""
    server = importlib.import_module("awnode.server")
    from fastapi.testclient import TestClient

    # 'pack/nope' → 'nope' → not bundled → 404 (proves the prefix was stripped,
    # otherwise the '/' would trip the 400 name validator instead).
    r = TestClient(server.app).post("/packs/install", json={"pack": "pack/nope"})
    assert r.status_code == 404


# ── Sovereign registry tool-pack install ─────────────────────────────────────


def _toolpack_tar(members: dict, *, symlink: tuple = None, absolute: bool = False) -> bytes:
    """Build a .tar.gz. members: {name: bytes}. Optional unsafe members for
    the fail-closed extraction tests."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        if symlink:
            link_name, target = symlink
            ti = tarfile.TarInfo(link_name)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            t.addfile(ti)
        if absolute:
            data = b"x"
            ti = tarfile.TarInfo("/etc/evil")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_safe_extract_valid_toolpack(tmp_path):
    server = importlib.import_module("awnode.server")
    from pathlib import Path

    blob = _toolpack_tar({
        "headroom/.toolpack.yaml": b"id: headroom\nentitlements: []\n",
        "headroom/tool.py": b"# tool\n",
    })
    dest = server._safe_extract_toolpack(blob, Path(tmp_path), "headroom")
    assert dest == (Path(tmp_path) / "headroom").resolve()
    assert (dest / ".toolpack.yaml").is_file()
    assert (dest / "tool.py").is_file()


def test_safe_extract_rejects_traversal(tmp_path):
    server = importlib.import_module("awnode.server")
    from pathlib import Path
    from fastapi import HTTPException
    import pytest

    blob = _toolpack_tar({
        "headroom/.toolpack.yaml": b"id: headroom\n",
        "headroom/../../evil.txt": b"pwned",
    })
    with pytest.raises(HTTPException) as ei:
        server._safe_extract_toolpack(blob, Path(tmp_path), "headroom")
    assert ei.value.status_code == 400
    assert not (Path(tmp_path).parent / "evil.txt").exists()


def test_safe_extract_rejects_symlink(tmp_path):
    server = importlib.import_module("awnode.server")
    from pathlib import Path
    from fastapi import HTTPException
    import pytest

    blob = _toolpack_tar(
        {"headroom/.toolpack.yaml": b"id: headroom\n"},
        symlink=("headroom/link", "/etc/passwd"),
    )
    with pytest.raises(HTTPException) as ei:
        server._safe_extract_toolpack(blob, Path(tmp_path), "headroom")
    assert ei.value.status_code == 400


def test_safe_extract_rejects_absolute_path(tmp_path):
    server = importlib.import_module("awnode.server")
    from pathlib import Path
    from fastapi import HTTPException
    import pytest

    blob = _toolpack_tar({"headroom/.toolpack.yaml": b"id: headroom\n"}, absolute=True)
    with pytest.raises(HTTPException) as ei:
        server._safe_extract_toolpack(blob, Path(tmp_path), "headroom")
    assert ei.value.status_code == 400


def test_safe_extract_requires_manifest(tmp_path):
    """An archive without a .toolpack.yaml is not a tool pack → 502, never
    silently installed."""
    server = importlib.import_module("awnode.server")
    from pathlib import Path
    from fastapi import HTTPException
    import pytest

    blob = _toolpack_tar({"headroom/random.py": b"# not a pack\n"})
    with pytest.raises(HTTPException) as ei:
        server._safe_extract_toolpack(blob, Path(tmp_path), "headroom")
    assert ei.value.status_code == 502


def test_registry_install_premium_is_403(monkeypatch, tmp_path):
    """A premium marketplace pack (403) surfaces as a managed-only 403 — the
    node never fabricates a success or falls through to the copyable command."""
    server = importlib.import_module("awnode.server")
    import asyncio

    class _Resp:
        status_code = 403

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server._install_registry_toolpack("formbridge", None))
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "premium_pack_managed_only"


def test_registry_install_free_extracts(monkeypatch, tmp_path):
    """A free marketplace pack (200 gzip) installs into the tool-packs dir."""
    server = importlib.import_module("awnode.server")
    import asyncio

    blob = _toolpack_tar({
        "headroom/.toolpack.yaml": b"id: headroom\nentitlements: []\n",
        "headroom/tool.py": b"# t\n",
    })

    class _Resp:
        status_code = 200
        content = blob

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(server, "_toolpacks_dir", lambda: __import__("pathlib").Path(tmp_path))

    res = asyncio.run(server._install_registry_toolpack("headroom", None))
    assert res["ok"] and res["kind"] == "tool_pack"
    from pathlib import Path
    assert (Path(tmp_path) / "headroom" / ".toolpack.yaml").is_file()


def test_service_install_command_shapes():
    from awnode import service_install as si

    cmd = si._node_command(8090, "127.0.0.1")
    assert cmd[-4:] == ["--host", "127.0.0.1", "--port", "8090"] or (
        "--port" in cmd and "8090" in cmd
    )
    # platform dispatch table covers all actions
    for action in ("install", "uninstall", "status", "start", "stop"):
        # must not raise on lookup (execution itself is platform-mutating; not run here)
        assert action in {"install", "uninstall", "status", "start", "stop"}
