"""awnode's fleet-verb MCP tools: argv over AitherOS/dev/tools/fleet_verbs.py, aliases, the
dry-run default, and a refusal passed through. Nothing here runs wsl.exe: subprocess is faked."""

import json

import pytest


@pytest.fixture()
def fv(monkeypatch, tmp_path):
    import awnode.tools.fleet_host as fh
    import awnode.tools.fleet_verbs as fv

    tool = tmp_path / "AitherOS" / "dev" / "tools" / "fleet_verbs.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("", encoding="utf-8")
    monkeypatch.setenv("AITHEROS_ROOT", str(tmp_path))
    calls = []
    reply = {"stdout": '{"verb": "x", "ok": true, "dry_run": true, "steps": []}', "rc": 0}

    class _P:
        stderr = ""

        @property
        def returncode(self):
            return reply["rc"]

        @property
        def stdout(self):
            return reply["stdout"]

    def fake_run(argv, **kw):
        calls.append(argv)
        return _P()

    monkeypatch.setattr(fh.subprocess, "run", fake_run)
    fv.calls, fv.reply, fv.tool = calls, reply, tool
    return fv


def test_registered_in_the_offline_scope():
    from awnode.tools._scopes import SHELL_LOCAL

    assert "fleet_verbs" in SHELL_LOCAL


@pytest.mark.parametrize("verb,execute,force,tail", [
    ("gpu-sleep", False, False, ["gpu", "sleep", "--json"]),
    ("gpu sleep", True, False, ["gpu", "sleep", "--execute", "--json"]),
    ("gaming", True, False, ["gpu", "sleep", "--execute", "--json"]),
    ("gpu-wake", True, True, ["gpu", "wake", "--execute", "--force", "--json"]),
    ("resume", True, False, ["gpu", "wake", "--execute", "--json"]),
    ("down", True, False, ["fleet", "sleep", "--execute", "--json"]),
    ("up", False, False, ["fleet", "wake", "--json"]),
    ("fleet-critical", True, False, ["fleet", "critical", "--execute", "--json"]),
    ("arc-status", True, False, ["arc", "status", "--json"]),
])
def test_verb_argv(fv, verb, execute, force, tail):
    doc = json.loads(fv.fleet_verb(verb, execute=execute, force=force))
    assert doc["ok"] is True
    argv = fv.calls[-1]
    assert argv[1] == str(fv.tool) and argv[2:] == tail


def test_status(fv):
    json.loads(fv.fleet_verbs_status())
    assert fv.calls[-1][2:] == ["status", "--json"]


def test_unknown_verb_never_runs(fv):
    doc = json.loads(fv.fleet_verb("fleet-explode", execute=True))
    assert doc["ok"] is False and not fv.calls


def test_gpu_blocked_refusal_passes_through(fv):
    fv.reply.update(rc=1, stdout=json.dumps({
        "verb": "gpu-wake", "ok": False, "rc": 1,
        "refused": "awnix reports \"GPU access blocked by the operating system\" -- ..."}))
    doc = json.loads(fv.fleet_verb("gpu-wake", execute=True))
    assert doc["ok"] is False and "GPU access blocked" in doc["refused"] and doc["rc"] == 1
