"""awnode's fleet-host surface: the MCP tools and the /status summary Awconnect reads.

Nothing here runs wsl.exe: the subprocess is faked.
"""

import json
import time

import pytest


@pytest.fixture()
def tools(monkeypatch, tmp_path):
    import awnode.tools.fleet_host as fh

    tool = tmp_path / "AitherOS" / "dev" / "tools" / "fleet_host.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("", encoding="utf-8")
    monkeypatch.setenv("AITHEROS_ROOT", str(tmp_path))
    calls = []

    class _P:
        returncode = 0
        stdout = '{"ok": true, "dry_run": true, "commands": ["x"]}'
        stderr = ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return _P()

    monkeypatch.setattr(fh.subprocess, "run", fake_run)
    fh.calls = calls
    return fh


def test_registered_in_the_offline_scope():
    from awnode.tools._scopes import SHELL_LOCAL

    assert "fleet_host" in SHELL_LOCAL


def test_status_wraps_the_engine(tools):
    doc = json.loads(tools.fleet_host_status())
    assert doc["ok"] and tools.calls[0][-2:] == ["status", "--json"]


def test_actions_are_dry_unless_execute(tools):
    json.loads(tools.fleet_host_action("restart"))
    assert "--execute" not in tools.calls[-1]
    json.loads(tools.fleet_host_action("stop", execute=True, terminate=True))
    assert "--execute" in tools.calls[-1] and "--terminate" in tools.calls[-1]


def test_live_migration_is_refused_over_mcp(tools):
    doc = json.loads(tools.fleet_host_action("migrate", execute=True))
    assert doc["refused"] and not tools.calls


def test_unknown_action_rejected(tools):
    assert not json.loads(tools.fleet_host_action("format-c"))["ok"]


def test_status_summary_reads_a_fresh_cache_only(monkeypatch, tmp_path):
    import awnode.server as server

    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    assert server._fleet_host_summary() is None
    (tmp_path / "fleet-host-status.json").write_text(
        json.dumps(
            {
                "distro": "aitheros-fleet",
                "verdict": "HEALTHY",
                "checked_epoch": time.time(),
                "secret_field": "x",
            }
        ),
        encoding="utf-8",
    )
    s = server._fleet_host_summary()
    assert s["distro"] == "aitheros-fleet" and "secret_field" not in s
    assert server._fleet_host_summary(now=time.time() + 3600) is None
