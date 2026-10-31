"""A hub brick's start failure must not hide its tools forever (gap row 32)."""
from __future__ import annotations

import awnode.hub as hub


def _bare_hub():
    h = object.__new__(hub.Hub)
    h.errors = {}
    h.error_at = {}
    return h


def test_a_fresh_error_suppresses_and_an_old_one_expires(monkeypatch):
    h = _bare_hub()
    now = [1000.0]
    monkeypatch.setattr(hub.time, "monotonic", lambda: now[0])
    h.errors["awm"] = "did not answer initialize"
    h.error_at["awm"] = now[0]
    assert h._error_fresh("awm") is True
    now[0] += hub.Hub.ERROR_COOLDOWN_S + 1
    assert h._error_fresh("awm") is False
    assert "awm" not in h.errors


def test_not_installed_stays_suppressed():
    h = _bare_hub()
    h.errors["x"] = "x is not installed on this machine"
    assert h._error_fresh("x") is True
