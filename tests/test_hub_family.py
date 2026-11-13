"""The hub's children come from the vendored family index, not a list in hub.py."""
from __future__ import annotations

import json
from pathlib import Path

import awnode.hub as hub
import pytest

MONOREPO_COPY = Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "family.json"


def _live_mcp_ids(doc: dict) -> set:
    return {b["id"] for b in doc["bricks"] for s in b.get("harness_surfaces") or []
            if s.get("harness") == "claude-code" and s.get("kind") == "mcp"
            and s.get("state") == "live"}


def test_shipped_index_yields_exactly_its_live_mcp_bricks():
    doc = json.loads(hub.FAMILY_JSON.read_text(encoding="utf-8"))
    names = [s["name"] for s in hub.family_bricks()]
    assert names and set(names) == _live_mcp_ids(doc)
    # module-launched aggregate servers lose tool-name collisions to brick-own servers
    kinds = [bool(s.get("module")) for s in hub.family_bricks()]
    assert kinds == sorted(kinds)


@pytest.mark.skipif(not MONOREPO_COPY.exists(), reason="monorepo copy absent (installed wheel)")
def test_vendored_copy_is_byte_identical_to_the_monorepo_index():
    assert hub.FAMILY_JSON.read_bytes() == MONOREPO_COPY.read_bytes()


def test_missing_index_is_loud(tmp_path):
    with pytest.raises(hub.FamilyIndexError):
        hub.family_bricks(tmp_path / "absent.json")


def test_run_lines_become_launch_specs():
    assert hub.spec_from_run("awm", "awm mcp") == {
        "name": "awm", "command": "awm", "args": ["mcp"], "run": "awm mcp"}
    sp = hub.spec_from_run("awdk", "python -m adk.harnesses.mcp_stdio")
    assert sp["module"] == "adk.harnesses.mcp_stdio" and sp["args"] == []


def test_isolated_tool_install_wins_over_the_shared_interpreter(tmp_path, monkeypatch):
    name = "awzz-hub-test.exe" if hub.os.name == "nt" else "awzz-hub-test"
    exe = tmp_path / name
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path))
    argv = hub.brick_argv({"name": "awzz", "command": "awzz-hub-test", "args": ["mcp"]})
    assert argv == [str(exe), "mcp"]


def test_absent_command_resolves_to_none(monkeypatch, tmp_path):
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path))
    assert hub.brick_argv({"name": "x", "command": "awzz-no-such-brick-zz", "args": []}) is None


def test_legacy_awsh_override_reaches_the_awdk_child():
    specs = hub.brick_specs({"bricks": {"awsh": {"disabled": True}}},
                            family=[{"name": "awdk", "module": "m"},
                                    {"name": "awm", "command": "awm", "args": ["mcp"]}])
    assert [s["name"] for s in specs] == ["awm"]


def test_readiness_names_why_a_brick_is_not_serving(tmp_path):
    h = hub.Hub([{"name": "ghost", "command": "awzz-no-such-brick-zz", "args": ["mcp"],
                  "run": "awzz-no-such-brick-zz mcp"}], tmp_path)
    row = h.readiness()["ghost"]
    assert row["installed"] is False and row["launched"] is False
    assert row["initialized"] is False and "not installed" in row["why"]
