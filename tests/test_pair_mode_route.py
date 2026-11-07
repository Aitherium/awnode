"""`awnode pair-mode` routes to adk.lan_pair before awnode's own parser and license gate."""

import sys
import types

from awnode import cli


def test_pair_mode_routes_to_adk(monkeypatch):
    seen = {}
    fake = types.ModuleType("adk.lan_pair")
    fake.main = lambda argv: seen.setdefault("argv", argv) and 7
    monkeypatch.setitem(sys.modules, "adk.lan_pair", fake)
    monkeypatch.setattr(sys, "argv", ["awnode", "pair-mode", "--class", "desktop"])
    assert cli.main() == 7
    assert seen["argv"] == ["--class", "desktop"]


def test_pair_mode_without_new_awdk_fails_closed(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "adk.lan_pair", None)  # import raises ImportError
    monkeypatch.setattr(sys, "argv", ["awnode", "pair-mode"])
    assert cli.main() == 1
    assert "newer awdk" in capsys.readouterr().out
