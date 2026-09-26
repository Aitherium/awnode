"""Route and version hygiene for awnode.server (w1b-19-12).

Two handlers once registered GET /mcp/tools; FastAPI serves the first, so the
second (Origin-checked) one was dead code that read as a live defense. The app
and NodeStatus versions were also hardcoded strings that drifted from the
package version.
"""

import importlib
from collections import Counter

import pytest


@pytest.fixture()
def server():
    return importlib.import_module("awnode.server")


def test_no_duplicate_routes(server):
    keys = Counter()
    for route in server.app.routes:
        methods = getattr(route, "methods", None) or ()
        for m in methods:
            keys[(m, getattr(route, "path", ""))] += 1
    dupes = {k: n for k, n in keys.items() if n > 1 and k[0] != "HEAD"}
    assert not dupes, f"routes registered more than once (only the first is served): {dupes}"


def test_versions_follow_the_package(server):
    import awnode

    assert server.app.version == awnode.__version__
    assert server.NodeStatus().version == awnode.__version__


def test_mcp_tools_refuses_a_foreign_browser_origin(server, monkeypatch):
    from fastapi.testclient import TestClient

    async def _noop_refresh():
        return None

    monkeypatch.setattr(server._state, "refresh", _noop_refresh)
    client = TestClient(server.app)
    r = client.get("/mcp/tools", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = client.get("/mcp/tools", headers={"Origin": "http://localhost:3000"})
    assert ok.status_code == 200
    plain = client.get("/mcp/tools")
    assert plain.status_code == 200


def _client(server, monkeypatch):
    from fastapi.testclient import TestClient

    async def _noop_refresh():
        return None

    monkeypatch.setattr(server._state, "refresh", _noop_refresh)
    return TestClient(server.app)


def test_mcp_tools_serves_the_awconnect_extension_origin(server, monkeypatch):
    # awconnect's MV3 service worker fetches /mcp/tools with a chrome-extension
    # Origin; refusing it made the node-only tier report "exposed no tools".
    monkeypatch.delenv("AWNODE_EXTENSION_IDS", raising=False)
    client = _client(server, monkeypatch)
    ext = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    assert client.get("/mcp/tools", headers={"Origin": ext}).status_code == 200


def test_mcp_tools_extension_pin_is_enforced(server, monkeypatch):
    monkeypatch.setenv("AWNODE_EXTENSION_IDS", "abcdefghijklmnopabcdefghijklmnop")
    client = _client(server, monkeypatch)
    ok = client.get("/mcp/tools", headers={"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"})
    assert ok.status_code == 200
    other = client.get("/mcp/tools", headers={"Origin": "chrome-extension://pppppppppppppppppppppppppppppppp"})
    assert other.status_code == 403


@pytest.mark.parametrize(
    "origin",
    ["null", "https://evilaitherium.com", "https://aitherium.com.evil.example", "file://", "data:text/html,x"],
)
def test_mcp_tools_refuses_null_and_lookalike_origins(server, monkeypatch, origin):
    client = _client(server, monkeypatch)
    assert client.get("/mcp/tools", headers={"Origin": origin}).status_code == 403


@pytest.mark.parametrize("origin", ["https://aitherium.com", "https://portal.aitherium.com", "http://127.0.0.1:8090"])
def test_mcp_tools_serves_first_party_web_origins(server, monkeypatch, origin):
    client = _client(server, monkeypatch)
    assert client.get("/mcp/tools", headers={"Origin": origin}).status_code == 200
