"""`awnode mcp --transport sse` end to end, over a real socket with the real MCP client.

The transport used to call ``SseServerTransport.get_starlette_app()``, which no mcp
1.x release has, so the SSE server died with AttributeError before binding, and its
``handle_sse`` was never routed and there was no ``/messages/`` route. A unit test of
the route table would not have caught that it ALSO has to stream; this drives
GET /sse + POST /messages/ with ``mcp.client.sse.sse_client`` and lists + calls a tool.
"""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest

from awnode import mcp_server as m

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_sse(monkeypatch):
    """Serve build_sse_app(key) on a loopback port in a thread; yield (base, key)."""
    servers = []

    def start(key: str = ""):
        monkeypatch.setattr(m, "_TOOL_REGISTRY", m._discover_tools())
        monkeypatch.setattr(m, "_current_mode", m.RuntimeMode.STANDALONE)
        port = _free_port()
        config = uvicorn.Config(m.build_sse_app(key), host="127.0.0.1", port=port, log_level="warning")
        srv = uvicorn.Server(config)
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        deadline = time.time() + 15
        while not srv.started:
            if time.time() > deadline:
                raise RuntimeError("SSE server did not start")
            time.sleep(0.05)
        servers.append((srv, t))
        return f"http://127.0.0.1:{port}"

    yield start
    for srv, t in servers:
        srv.should_exit = True
        t.join(timeout=10)


def test_sse_transport_lists_and_calls_tools(live_sse, tmp_path):
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    base = live_sse("")
    probe = tmp_path / "exists.txt"
    probe.write_text("x", encoding="utf-8")

    async def drive():
        async with sse_client(f"{base}/sse") as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 15)
                tools = await asyncio.wait_for(session.list_tools(), 15)
                names = {t.name for t in tools.tools}
                result = await asyncio.wait_for(
                    session.call_tool("file_exists", {"path": str(probe)}), 15
                )
                return names, result

    names, result = asyncio.run(drive())
    assert "file_exists" in names and "read_file" in names
    payload = json.loads(result.content[0].text)
    assert payload.get("exists") is True


def test_sse_health_and_bearer_gate(live_sse):
    base = live_sse("s3cret")
    assert httpx.get(f"{base}/health", timeout=5).status_code == 200
    # Both MCP routes sit behind the key.
    assert httpx.get(f"{base}/sse", timeout=5).status_code == 401
    assert httpx.post(f"{base}/messages/?session_id=0", json={}, timeout=5).status_code == 401
    assert httpx.get(f"{base}/sse", headers={"Authorization": "Bearer nope"}, timeout=5).status_code == 401
    # The right key opens the stream (read only the status line, then hang up).
    with httpx.stream("GET", f"{base}/sse", headers={"Authorization": "Bearer s3cret"}, timeout=5) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")


def test_sse_route_is_get_only(live_sse):
    base = live_sse("")
    assert httpx.post(f"{base}/sse", json={}, timeout=5).status_code == 405


@pytest.mark.parametrize("key", ["", "s3cret"])
def test_sse_refuses_browser_origins_and_foreign_hosts(live_sse, key):
    # DNS rebinding: a page on evil.example re-pointed at 127.0.0.1 is same-origin to
    # itself, so it can open /sse, read the session endpoint and POST run_command.
    # With no AITHER_MCP_KEY (allowed on a loopback bind) the Origin/Host guard is
    # the only thing in the way; it must answer 403 before any session lookup.
    base = live_sse(key)
    auth = {"Authorization": f"Bearer {key}"} if key else {}
    evil = {"Host": "evil.example:8090", "Origin": "http://evil.example:8090"}
    cases = [
        {"Host": "evil.example:8090"},
        {"Origin": "http://evil.example:8090"},
        {"Origin": "chrome-extension://hlmfknhcfhjjngckfpacgleffckpmphe"},
        evil,
    ]
    for h in cases:
        assert httpx.get(f"{base}/sse", headers={**auth, **h}, timeout=5).status_code == 403, h
        r = httpx.post(f"{base}/messages/?session_id=0123456789abcdef0123456789abcdef",
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={**auth, **h}, timeout=5)
        assert r.status_code == 403, h
    # A real session id does not help a rebinding page either.
    with httpx.stream("GET", f"{base}/sse", headers=auth, timeout=5) as stream:
        assert stream.status_code == 200
        endpoint = None
        for line in stream.iter_lines():
            if line.startswith("data:"):
                endpoint = line.split(":", 1)[1].strip()
                break
        assert endpoint and "session_id=" in endpoint
        r = httpx.post(f"{base}{endpoint}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={**auth, **evil}, timeout=5)
        assert r.status_code == 403


def test_sse_loopback_host_spellings_are_accepted():
    assert m._host_is_loopback("127.0.0.1:8090")
    assert m._host_is_loopback("localhost")
    assert m._host_is_loopback("[::1]:8090")
    assert not m._host_is_loopback("evil.example:8090")
    assert not m._host_is_loopback("127.0.0.1.evil.example")
    assert not m._host_is_loopback("")
