"""Security tests for AwNode read-only MCP surface (hardening PR #2066).

Tests verify:
1. Spoofed Origin from non-loopback peer WITHOUT bearer token is REJECTED (vuln closure)
2. File access is confined to AITHERNODE_FS_ROOT, cannot escape to parent/home
3. X-Forwarded-For spoofing does NOT grant access (only direct peer check matters)
4. Loopback clients are permitted without bearer token (dev convenience)
5. Non-loopback clients with valid bearer token are permitted
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import pytest
from fastapi import HTTPException

# Ensure we use the local awnode package
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def temp_aither_home(monkeypatch):
    """Create a temporary ~/.aither directory with sync_state.json containing a bearer token."""
    with tempfile.TemporaryDirectory() as tmpdir:
        aither_home = Path(tmpdir) / ".aither"
        aither_home.mkdir(parents=True, exist_ok=True)

        # Create sync_state.json with a known bearer token
        sync_state = {
            "tenant_scoped_token": "test-bearer-secret-12345",
            "node_id": "test-node-1",
        }
        (aither_home / "sync_state.json").write_text(json.dumps(sync_state))

        # Set AITHER_HOME to the temp directory
        monkeypatch.setenv("AITHER_HOME", str(aither_home))

        yield aither_home, sync_state


def test_get_enrolled_bearer_token(temp_aither_home, monkeypatch):
    """Test that _get_enrolled_bearer_token reads the token from sync_state.json."""
    aither_home, sync_state = temp_aither_home

    from awnode.server import _get_enrolled_bearer_token

    token = _get_enrolled_bearer_token()
    assert token == sync_state["tenant_scoped_token"]


def test_get_enrolled_bearer_token_missing(monkeypatch):
    """Test that _get_enrolled_bearer_token returns None when sync_state.json doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("AITHER_HOME", tmpdir)

        from awnode.server import _get_enrolled_bearer_token

        token = _get_enrolled_bearer_token()
        assert token is None


def test_is_loopback_client():
    """Test that _is_loopback_client correctly identifies loopback addresses."""
    from awnode.server import _is_loopback_client

    class MockRequest:
        def __init__(self, host):
            self.client = type("Client", (), {"host": host})()

    # IPv4 loopback
    assert _is_loopback_client(MockRequest("127.0.0.1")) is True

    # IPv6 loopback
    assert _is_loopback_client(MockRequest("::1")) is True

    # localhost
    assert _is_loopback_client(MockRequest("localhost")) is True

    # Non-loopback
    assert _is_loopback_client(MockRequest("192.168.1.1")) is False
    assert _is_loopback_client(MockRequest("10.0.0.1")) is False

    # No client
    req = MockRequest("127.0.0.1")
    req.client = None
    assert _is_loopback_client(req) is False


def test_check_file_access_auth_loopback(temp_aither_home):
    """Test that loopback clients are always allowed, even without bearer token."""
    from awnode.server import _check_file_access_auth

    class MockRequest:
        def __init__(self, host, auth=""):
            self.client = type("Client", (), {"host": host})()
            self.headers = {"authorization": auth}

    # Loopback without token should be allowed
    req = MockRequest("127.0.0.1")
    assert _check_file_access_auth(req) is True

    req = MockRequest("::1")
    assert _check_file_access_auth(req) is True

    req = MockRequest("localhost")
    assert _check_file_access_auth(req) is True


def test_check_file_access_auth_nonloopback_without_token(temp_aither_home):
    """Test that NON-LOOPBACK clients WITHOUT bearer token are REJECTED.

    THIS IS THE CRITICAL VULN CLOSURE: Before the fix, Origin header alone
    would allow access. Now a non-loopback client MUST present a bearer token.
    """
    from awnode.server import _check_file_access_auth

    class MockRequest:
        def __init__(self, host, auth=""):
            self.client = type("Client", (), {"host": host})()
            self.headers = {"authorization": auth}

    # Non-loopback without token -> REJECTED
    req = MockRequest("192.168.1.1", "")
    assert _check_file_access_auth(req) is False

    req = MockRequest("10.0.0.1", "")
    assert _check_file_access_auth(req) is False


def test_check_file_access_auth_nonloopback_with_valid_token(
    temp_aither_home,
):
    """Test that NON-LOOPBACK clients WITH valid bearer token are ALLOWED."""
    aither_home, sync_state = temp_aither_home

    from awnode.server import _check_file_access_auth

    class MockRequest:
        def __init__(self, host, auth=""):
            self.client = type("Client", (), {"host": host})()
            self.headers = {"authorization": auth}

    # Non-loopback with valid token -> ALLOWED
    req = MockRequest("192.168.1.1", f"Bearer {sync_state['tenant_scoped_token']}")
    assert _check_file_access_auth(req) is True

    req = MockRequest("10.0.0.1", f"Bearer {sync_state['tenant_scoped_token']}")
    assert _check_file_access_auth(req) is True


def test_check_file_access_auth_nonloopback_with_wrong_token(temp_aither_home):
    """Test that NON-LOOPBACK clients WITH wrong bearer token are REJECTED."""
    from awnode.server import _check_file_access_auth

    class MockRequest:
        def __init__(self, host, auth=""):
            self.client = type("Client", (), {"host": host})()
            self.headers = {"authorization": auth}

    # Non-loopback with wrong token -> REJECTED
    req = MockRequest("192.168.1.1", "Bearer wrong-token-xxxxx")
    assert _check_file_access_auth(req) is False


def test_check_file_access_auth_malformed_header(temp_aither_home):
    """Test that malformed Authorization headers are rejected."""
    from awnode.server import _check_file_access_auth

    class MockRequest:
        def __init__(self, host, auth=""):
            self.client = type("Client", (), {"host": host})()
            self.headers = {"authorization": auth}

    # Malformed: no "Bearer " prefix
    req = MockRequest("192.168.1.1", "test-bearer-secret-12345")
    assert _check_file_access_auth(req) is False

    # Malformed: wrong scheme
    req = MockRequest("192.168.1.1", "Basic dGVzdDp0ZXN0")
    assert _check_file_access_auth(req) is False

    # Malformed: empty token
    req = MockRequest("192.168.1.1", "Bearer ")
    assert _check_file_access_auth(req) is False


def test_normalize_path_confined_to_root():
    """Test that _normalize_path prevents escaping the filesystem root."""
    import tempfile

    from awnode.server import _normalize_path

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "allowed_root"
        root.mkdir(parents=True, exist_ok=True)
        outside = Path(tmpdir) / "outside.txt"
        outside.write_text("outside file")

        # Create an inside file
        (root / "inside.txt").write_text("inside file")

        # Patch the global AITHERNODE_FS_ROOT
        import awnode.server as server_module
        original_root = server_module.AITHERNODE_FS_ROOT
        try:
            server_module.AITHERNODE_FS_ROOT = root

            # Inside file -> allowed
            resolved, is_valid = _normalize_path("inside.txt")
            assert is_valid is True
            assert "inside.txt" in str(resolved)

            # Parent escape -> denied
            resolved, is_valid = _normalize_path("../outside.txt")
            assert is_valid is False

            # Absolute path outside root -> denied
            resolved, is_valid = _normalize_path(str(outside))
            assert is_valid is False

        finally:
            server_module.AITHERNODE_FS_ROOT = original_root


def test_check_origin_validation():
    """Test that _check_origin correctly validates origin headers."""
    from awnode.server import _check_origin

    class MockRequest:
        def __init__(self, origin=""):
            self.headers = {"origin": origin}

    # Valid origins
    assert _check_origin(MockRequest("https://portal.aitherium.com")) is True
    assert _check_origin(MockRequest("https://subdomain.aitherium.com")) is True
    assert _check_origin(MockRequest("http://localhost:3000")) is True
    assert _check_origin(MockRequest("http://127.0.0.1:8090")) is True

    # Invalid origins
    assert _check_origin(MockRequest("https://evil.com")) is False
    assert _check_origin(MockRequest("")) is False
    # Note: ftp://aitherium.com has hostname "aitherium.com" so it passes
    # (scheme isn't checked, only hostname). This is OK since origin is just
    # defense-in-depth; the real security is the bearer token check.


def test_xforwarded_for_not_trusted():
    """Test that X-Forwarded-For header does NOT affect loopback check.

    This proves that only request.client.host (the direct peer) matters,
    not X-Forwarded-For, preventing spoofing by non-browser clients.
    """
    from awnode.server import _is_loopback_client

    class MockRequest:
        def __init__(self, client_host, x_forwarded_for=""):
            self.client = type("Client", (), {"host": client_host})()
            self.headers = {}
            if x_forwarded_for:
                self.headers["x-forwarded-for"] = x_forwarded_for

    # Direct peer is loopback -> allowed (even if X-Forwarded-For says otherwise)
    req = MockRequest("127.0.0.1", "10.0.0.1")
    assert _is_loopback_client(req) is True

    # Direct peer is non-loopback -> NOT allowed (even if X-Forwarded-For says loopback)
    req = MockRequest("192.168.1.1", "127.0.0.1")
    assert _is_loopback_client(req) is False


class _MockExecRequest:
    """Minimal stand-in for a Starlette Request for the /mcp/execute handler."""

    def __init__(self, client_host="127.0.0.1", origin=None):
        self.client = type("Client", (), {"host": client_host})()
        self.headers = {}
        if origin is not None:
            self.headers["origin"] = origin


def _call_execute(tool, arguments, client_host="127.0.0.1", origin=None):
    import asyncio
    from awnode.server import mcp_execute, MCPToolRequest

    req = MCPToolRequest(tool=tool, arguments=arguments)
    http_req = _MockExecRequest(client_host=client_host, origin=origin)
    return asyncio.run(mcp_execute(req, http_req))


def test_drive_by_foreign_origin_file_read_is_denied(temp_aither_home):
    """A loopback browser request (evil.com) MUST be denied for file tools.

    THIS IS THE CRITICAL DRIVE-BY CLOSURE: every browser request arrives from
    127.0.0.1, so the loopback rule alone would let ANY site read AitherShared.
    A foreign Origin header identifies the cross-origin browser caller -> 403.
    """
    with pytest.raises(HTTPException) as exc:
        _call_execute("read_file", {"path": "README.txt"},
                      client_host="127.0.0.1", origin="https://evil.com")
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        _call_execute("list_dir", {"path": "."},
                      client_host="127.0.0.1", origin="https://evil.com")
    assert exc.value.status_code == 403


def test_aitherium_origin_file_read_passes_origin_gate(temp_aither_home):
    """A loopback browser request from aitherium.com passes the origin gate.

    It should NOT raise a 403 for origin; it proceeds to the read (which may
    then legitimately fail with a not-found result, but never an origin 403).
    """
    result = _call_execute("read_file", {"path": "does-not-exist.txt"},
                           client_host="127.0.0.1", origin="https://portal.aitherium.com")
    # Not a 403 — a structured tool result (success False, not-found), origin allowed.
    assert isinstance(result, dict)
    assert result.get("success") is False


def test_origin_less_local_tool_still_allowed(temp_aither_home):
    """An origin-less loopback caller (curl / local CLI) is still allowed through."""
    result = _call_execute("list_dir", {"path": "."},
                           client_host="127.0.0.1", origin=None)
    assert isinstance(result, dict)
    assert result.get("success") is True
