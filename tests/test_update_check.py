"""awnode's once-a-day PyPI update check (awnode/update_check.py).

awnode had no update check: nothing on the CLI or GET /status said a newer
release existed. These pin the lookup target, the cache, the opt-out, the
never-block contract and the /status field.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from awnode import update_check as uc  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for k in ("AWNODE_NO_UPDATE_CHECK", "AITHER_NO_UPDATE_CHECK", "AITHER_OFFLINE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setattr(uc, "upgrade_command", lambda: "pip install --upgrade awnode")
    monkeypatch.setattr(uc, "_thread", None)


def _pypi(monkeypatch, latest="9.9.9", delay=0.0):
    seen = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"info": {"version": latest}}).encode()

    def _urlopen(url, timeout=None):
        seen.append(url)
        if delay:
            time.sleep(delay)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return seen


def _seed(latest, age=0.0):
    uc.cache_path().write_text(json.dumps({
        "package": "awnode", "latest_version": latest, "checked_at": time.time() - age}))


def test_refresh_asks_pypi_for_awnode_and_caches(monkeypatch):
    seen = _pypi(monkeypatch)
    t = uc.refresh_async()
    t.join(5)
    assert seen == ["https://pypi.org/pypi/awnode/json"]
    s = uc.status(current="0.3.2")
    assert s["latest"] == "9.9.9" and s["update_available"] is True
    assert s["upgrade_command"] == "pip install --upgrade awnode"
    assert uc.refresh_async() is None  # fresh cache: no second lookup today


def test_stale_cache_refreshes(monkeypatch):
    _seed("0.3.3", age=uc.CHECK_INTERVAL + 5)
    seen = _pypi(monkeypatch)
    uc.refresh_async().join(5)
    assert len(seen) == 1
    assert uc.status(current="0.3.2")["latest"] == "9.9.9"


def test_no_update_when_current_is_newer_or_equal():
    _seed("0.3.2")
    assert uc.status(current="0.3.2")["update_available"] is False
    assert uc.status(current="0.4.0")["update_available"] is False
    assert uc.notice(current="0.4.0") is None


@pytest.mark.parametrize("var", ["AWNODE_NO_UPDATE_CHECK", "AITHER_NO_UPDATE_CHECK", "AITHER_OFFLINE"])
def test_opt_out_never_dials(monkeypatch, capsys, var):
    seen = _pypi(monkeypatch)
    _seed("9.9.9")
    monkeypatch.setenv(var, "1")
    assert uc.refresh_async() is None
    uc.cli_start_check()
    assert seen == []
    assert uc.status() == {"enabled": False, "current": uc.__version__}
    assert capsys.readouterr().err == ""


def test_cli_prints_cached_notice_without_dialing(monkeypatch, capsys):
    seen = _pypi(monkeypatch)
    _seed("9.9.9")
    uc.cli_start_check()
    err = capsys.readouterr().err
    assert "awnode 9.9.9 is available" in err and "pip install --upgrade awnode" in err
    assert seen == []


def test_cli_start_never_blocks_on_a_slow_pypi(monkeypatch):
    _pypi(monkeypatch, delay=2.0)
    t0 = time.monotonic()
    uc.cli_start_check()
    assert time.monotonic() - t0 < 0.5
    uc._thread.join(5)  # let it land inside this test's AITHER_HOME, not the real one


def test_network_failure_is_silent(monkeypatch):
    def _boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    uc.refresh_async().join(5)
    assert uc.read_cache() is None  # an attempt stamp is not a version
    assert uc.status()["latest"] is None
    # ...but it is recorded, so the next /status poll does not ask again today.
    assert uc.refresh_async() is None


def test_offline_node_is_not_reasked_on_every_status_poll(monkeypatch):
    from fastapi.testclient import TestClient

    from awnode import server

    async def _noop_refresh():
        return None
    monkeypatch.setattr(server._state, "refresh", _noop_refresh)
    calls = []

    def _boom(*a, **k):
        calls.append(1)
        raise OSError("no egress")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    client = TestClient(server.app)
    for _ in range(3):
        assert client.get("/status").status_code == 200
        if uc._thread is not None:
            uc._thread.join(5)
    assert len(calls) == 1


def test_failed_refresh_keeps_the_last_known_version(monkeypatch):
    _seed("9.9.9", age=uc.CHECK_INTERVAL + 5)

    def _boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    uc.refresh_async().join(5)
    assert uc.status(current="0.3.2")["latest"] == "9.9.9"
    assert uc.refresh_async() is None


def test_attempt_stamp_expires_after_the_interval(monkeypatch):
    uc.cache_path().write_text(json.dumps({
        "package": "awnode", "attempted_at": time.time() - uc.CHECK_INTERVAL - 5}))
    seen = _pypi(monkeypatch)
    uc.refresh_async().join(5)
    assert len(seen) == 1


@pytest.mark.parametrize("prefix,container,cmd", [
    ("/home/u/.local/share/pipx/venvs/awnode", False, "pipx upgrade awnode"),
    ("/home/u/.local/share/uv/tools/awnode", False, "uv tool upgrade awnode"),
    ("/usr", False, "pip install --upgrade awnode"),
    ("/usr", True, "pull/rebuild the awnode image"),
])
def test_upgrade_command_per_install_method(monkeypatch, prefix, container, cmd):
    monkeypatch.undo()
    assert uc.upgrade_command(prefix, in_container=container).startswith(cmd)


@pytest.mark.parametrize("marker", ["/.dockerenv", "/run/.containerenv"])
def test_container_detection_covers_docker_and_podman(monkeypatch, marker):
    monkeypatch.undo()
    monkeypatch.delenv("AWNODE_IN_CONTAINER", raising=False)
    monkeypatch.setattr(uc.Path, "exists",
                        lambda self: self.as_posix() == marker)
    assert uc.upgrade_command("/usr").startswith("pull/rebuild the awnode image")


def test_container_detection_env_override(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(uc.Path, "exists", lambda self: False)
    monkeypatch.setenv("AWNODE_IN_CONTAINER", "1")
    assert uc.upgrade_command("/usr").startswith("pull/rebuild the awnode image")
    monkeypatch.delenv("AWNODE_IN_CONTAINER")
    assert uc.upgrade_command("/usr") == "pip install --upgrade awnode"


def test_status_route_reports_update(monkeypatch):
    from fastapi.testclient import TestClient

    from awnode import server

    async def _noop_refresh():
        return None
    monkeypatch.setattr(server._state, "refresh", _noop_refresh)
    seen = _pypi(monkeypatch)
    _seed("9.9.9")
    body = TestClient(server.app).get("/status").json()
    assert body["update"]["latest"] == "9.9.9"
    assert body["update"]["update_available"] is True
    assert seen == []  # the route reads the cache, it never waits on pypi.org
