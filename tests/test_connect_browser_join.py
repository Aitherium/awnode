"""``awnode connect`` with no token goes to the browser device flow, then enroll-self.

Both halves mocked: the adk path (one implementation with ``adk join --workspace``) and
the stripped-binary path (stdlib device flow + enroll-self + member settings pull).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from awnode import cli


def test_no_token_dispatches_to_the_browser_flow(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_connect_browser", lambda portal: seen.setdefault("p", portal)
                        or 0)
    monkeypatch.setattr(sys, "argv", ["awnode", "connect"])
    monkeypatch.delenv("AITHER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 0 and seen["p"] == ""


def test_with_adk_it_is_adk_join():
    calls = []
    assert cli._connect_browser(join=lambda ws, label="": calls.append((ws, label)) or 0) == 0
    assert calls == [("", "awnode")]


def test_standalone_path_runs_device_flow_then_enroll_self_then_pull(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    posts, flows = [], []

    def post(url, body):
        posts.append((url, body))
        return SimpleNamespace(status_code=201, json=lambda: {"device_id": "dev1"})

    # join=None would import adk; force the standalone branch explicitly.
    monkeypatch.setitem(sys.modules, "adk.invite_enroll", None)
    rc = cli._connect_browser(
        "https://genesis.example",
        device_flow=lambda u: flows.append(u) or {"access_token": "user-session"},
        post=post, pull=lambda: 3)
    assert rc == 0
    assert len(flows) == 1
    assert posts == [("https://genesis.example" + cli.ENROLL_SELF_PATH, posts[0][1])]
    assert posts[0][1]["label"] == "awnode"


def test_standalone_refused_enroll_is_exit_1(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "adk.invite_enroll", None)
    rc = cli._connect_browser(
        device_flow=lambda u: {"access_token": "user-session"},
        post=lambda url, body: SimpleNamespace(status_code=403, json=dict),
        pull=lambda: 0)
    assert rc == 1


def test_enroll_token_path_is_kept():
    import inspect
    src = inspect.getsource(cli._connect)
    assert "enrollment-token/exchange" in src
