"""`awnode mcp` must leave STANDALONE when a backend comes up (gap row 30)."""
from __future__ import annotations

import asyncio

import awnode.mcp_server as ms


class _Session:
    def __init__(self):
        self.sent = 0

    async def send_tool_list_changed(self):
        self.sent += 1


def test_reprobe_switches_mode_and_notifies(monkeypatch):
    monkeypatch.setattr(ms, "_current_mode", ms.RuntimeMode.STANDALONE)
    sess = _Session()
    monkeypatch.setattr(ms, "_session", sess)

    async def up():
        return ms.RuntimeMode.LOCAL

    monkeypatch.setattr(ms, "detect_mode", up)
    assert asyncio.run(ms.reprobe_once()) is True
    assert ms._current_mode is ms.RuntimeMode.LOCAL
    assert sess.sent == 1
    # No change, no notification.
    assert asyncio.run(ms.reprobe_once()) is False
    assert sess.sent == 1
