"""
Fleet Host Tools -- the awnix fleet host (WSL2) as MCP tools
=============================================================

Wraps the AitherOS repo's ``AitherOS/dev/tools/fleet_host.py`` -- the one engine
that ``adk fleet-host``, awdesk's Fleet window and the AitherZero
``setup-awnix-fleet-host`` playbook also call -- so an agent sees the same
verdict and runs the same actions as every other surface.

awnode never imports AitherOS code: the tool runs as a subprocess from the repo
at ``$AITHEROS_ROOT`` / ``$AITHEROS_REPO``, the CWD, or a checkout discovered
at a drive root (never a hardcoded path). Every action is a DRY RUN unless
``execute=True``; a LIVE migration is refused here (run the playbook itself).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

_TOOL_REL = Path("AitherOS") / "dev" / "tools" / "fleet_host.py"
_ACTIONS = ("start", "stop", "restart", "reattach", "migrate")
_MODES = ("preflight", "rehearse", "cutover")

__all__ = ["fleet_host_status", "fleet_host_action"]


def _drive_roots() -> List[Path]:
    if os.name != "nt":
        return [Path("/")]
    import string

    return [Path(f"{d}:/") for d in string.ascii_uppercase if Path(f"{d}:/").exists()]


def _find_tool(rel: Path = _TOOL_REL) -> Optional[Path]:
    cands: List[Path] = []
    for var in ("AITHEROS_ROOT", "AITHEROS_REPO"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]))
    cands.append(Path.cwd())
    cands += [r / n for r in _drive_roots() for n in ("AitherOS-Fresh", "AitherOS")]
    for c in cands:
        if (c / rel).is_file():
            return c / rel
    return None


def _run(args: List[str], timeout: int, rel: Path = _TOOL_REL) -> str:
    tool = _find_tool(rel)
    if tool is None:
        return json.dumps(
            {
                "ok": False,
                "cannot_judge": True,
                "error": f"{rel.name} not found; set AITHEROS_ROOT to the AitherOS checkout",
            }
        )
    try:
        p = subprocess.run(
            [sys.executable, str(tool), *args, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(tool.parents[3]),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"ok": False, "cannot_judge": True, "error": f"timed out after {timeout}s"}
        )
    out = p.stdout or ""
    i = out.find("{")
    if i < 0:
        return json.dumps(
            {
                "ok": False,
                "cannot_judge": True,
                "rc": p.returncode,
                "error": (p.stderr or out).strip()[-500:] or f"exit {p.returncode}",
            }
        )
    try:
        doc = json.loads(out[i:])
    except ValueError:
        return json.dumps({"ok": False, "cannot_judge": True, "error": out[-500:]})
    doc["rc"] = p.returncode
    return json.dumps(doc)


def fleet_host_status() -> str:
    """Report the awnix fleet host: distro, WSL state, systemd, podman, data attach, tiers, tasks.

    Read-only: never boots a stopped distro.

    Returns:
        JSON with verdict (HEALTHY | UNHEALTHY | CANNOT_JUDGE), problems and warnings.
    """
    return _run(["status"], timeout=180)


def fleet_host_action(
    action: str,
    execute: bool = False,
    force: bool = False,
    terminate: bool = False,
    mode: str = "preflight",
) -> str:
    """Run a fleet-host action: start, stop, restart, reattach or migrate. DRY RUN unless execute.

    Args:
        action: start | stop | restart | reattach | migrate.
        execute: actually run it (default False prints the exact commands).
        force: override the one-distro / maintenance-lock refusals.
        terminate: stop only -- also `wsl --terminate` the distro.
        mode: migrate only -- preflight | rehearse | cutover (always a dry run here).

    Returns:
        JSON with the commands, per-command results and ok.
    """
    if action not in _ACTIONS:
        return json.dumps(
            {"ok": False, "error": f"action must be one of {', '.join(_ACTIONS)}"}
        )
    args = [action]
    if action == "migrate":
        if execute:
            return json.dumps(
                {
                    "ok": False,
                    "refused": True,
                    "error": "a LIVE migration is not run from an MCP call; run the "
                    "migrate-fleet-to-awnix playbook (dry run is allowed here)",
                }
            )
        if mode not in _MODES:
            return json.dumps(
                {"ok": False, "error": f"mode must be one of {', '.join(_MODES)}"}
            )
        args += ["--mode", mode]
    if execute:
        args.append("--execute")
    if force:
        args.append("--force")
    if terminate and action == "stop":
        args.append("--terminate")
    return _run(args, timeout=7200 if action == "migrate" else 1200)
