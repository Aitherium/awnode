"""`awnode mcp` must import and serve on whichever mcp generation is installed.

2026-10-14: a sibling aw brick pulled mcp 2.2 into the shared env and `awnode mcp`
died at import (`'Server' object has no attribute 'list_tools'`). The live SSE test
proves list+call end to end; these pin the transport-neutral core both API paths wrap.
"""
from __future__ import annotations

import asyncio
import json

import awnode.mcp_server as ms


def test_module_builds_a_server_on_the_installed_mcp():
    assert ms.server is not None
    # 2.x registers on_list_tools via the constructor; 1.x via decorators. Either
    # way a tools/list handler must exist, or the server answers nothing.
    handlers = getattr(ms.server, "request_handlers", None)
    if handlers is not None:  # mcp 1.x
        assert any("ListTools" in getattr(k, "__name__", "") for k in handlers)
    else:  # mcp 2.x
        assert ms.server.get_request_handler("tools/list") is not None
        assert ms.server.get_request_handler("tools/call") is not None


def test_unknown_tool_is_an_error(monkeypatch):
    monkeypatch.setattr(ms, "_TOOL_REGISTRY", {})
    text, is_error = asyncio.run(ms.dispatch_tool("nope", {}))
    assert is_error is True
    assert "Unknown tool" in json.loads(text)["error"]


def test_scope_refusal_is_an_error_and_hidden_from_the_list(monkeypatch):
    def _f():
        return "ran"

    monkeypatch.setattr(ms, "_TOOL_REGISTRY", {"plat": {
        "func": _f, "scope": "platform_only", "module": "x",
        "description": "d", "schema": {"type": "object", "properties": {}},
    }})
    monkeypatch.setattr(ms, "_current_mode", ms.RuntimeMode.STANDALONE)
    assert [t.name for t in ms.visible_tools()] == []
    text, is_error = asyncio.run(ms.dispatch_tool("plat", {}))
    assert is_error is True and json.loads(text)["scope"] == "platform_only"


def test_allowed_tool_runs(monkeypatch):
    def _f(x: str = "") -> str:
        return f"got {x}"

    # "filesystem" is a free-plan module, so the subscription gate passes.
    monkeypatch.setattr(ms, "_TOOL_REGISTRY", {"echo": {
        "func": _f, "scope": "shell_local", "module": "filesystem",
        "description": "d", "schema": {"type": "object", "properties": {}},
    }})
    monkeypatch.setattr(ms, "_current_mode", ms.RuntimeMode.STANDALONE)
    assert [t.name for t in ms.visible_tools()] == ["echo"]
    text, is_error = asyncio.run(ms.dispatch_tool("echo", {"x": "hi", "junk": 1}))
    assert (text, is_error) == ("got hi", False)
