"""`awnode ext install` exits non-zero on failure; `ext list --json` is machine-readable.

A failed install used to print "Error: ..." and exit 0, so a caller that drives awnode
by exit code (adk's add-on resolver) read every failed install as a success.
"""
from __future__ import annotations

import asyncio
import json
import types

import awnode.extensions as ext_mod

from awnode import cli


class _Mgr:
    def __init__(self, install_result):
        self._install = install_result

    def list_catalog(self):
        return [{"id": "canvas", "name": "AitherCanvas", "description": "images"}]

    async def install(self, ext_id):
        return self._install


def _run(monkeypatch, capsys, mgr, **ns):
    monkeypatch.setattr(ext_mod, "get_extension_manager", lambda: mgr)
    rc = asyncio.run(cli._ext(types.SimpleNamespace(**ns)))
    return rc, capsys.readouterr().out


def test_failed_install_exits_1(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, _Mgr({"error": "no image", "hint": "pull it"}),
                   ext_command="install", extension_id="canvas", json=False)
    assert rc == 1 and "Error: no image" in out


def test_failed_install_json(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, _Mgr({"error": "no image"}),
                   ext_command="install", extension_id="canvas", json=True)
    obj = json.loads(out)
    assert rc == 1 and obj["ok"] is False and obj["id"] == "canvas"


def test_good_install_exits_0(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, _Mgr({"status": "installed"}),
                   ext_command="install", extension_id="canvas", json=False)
    assert rc == 0 and "Installed: canvas" in out


def test_list_json(monkeypatch, capsys):
    rc, out = _run(monkeypatch, capsys, _Mgr({}), ext_command="list", json=True)
    obj = json.loads(out)
    assert rc == 0 and obj["ok"] is True and obj["extensions"][0]["id"] == "canvas"
