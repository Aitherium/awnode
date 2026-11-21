"""
Fleet Verb Tools -- the owner's fleet verbs as MCP tools
=========================================================

gpu sleep | gpu wake | fleet sleep | fleet wake | fleet critical | arc start|stop|status,
and the verb set's status. Wraps the AitherOS repo's ``AitherOS/dev/tools/fleet_verbs.py``
-- the one implementation awdesk's Fleet window, ``adk gpu|fleet``, the ``aither`` CLI and
AitherZero also run -- so an agent's "gpu sleep" is the owner's "gpu sleep".

awnode never imports AitherOS code: the tool runs as a subprocess from the checkout
``fleet_host._find_tool`` finds. Every mutating verb is a DRY RUN unless ``execute=True``.
"""

from __future__ import annotations

import json
from pathlib import Path

from awnode.tools.fleet_host import _run

_TOOL_REL = Path("AitherOS") / "dev" / "tools" / "fleet_verbs.py"
#: the owner-facing verbs, and the old names that alias them
VERBS = ("gpu-sleep", "gpu-wake", "fleet-sleep", "fleet-wake", "fleet-critical",
         "arc-start", "arc-stop", "arc-status")
ALIASES = {"gaming": "gpu-sleep", "resume": "gpu-wake", "down": "fleet-sleep",
           "up": "fleet-wake", "critical": "fleet-critical"}
_TIMEOUT = {"gpu-wake": 5400, "fleet-wake": 5400, "fleet-critical": 2400}

__all__ = ["fleet_verbs_status", "fleet_verb"]


def verb_argv(verb: str, execute: bool = False, force: bool = False) -> list:
    """fleet_verbs.py argv (before --json) for a verb; ValueError if unknown."""
    v = str(verb or "").strip().lower().replace(" ", "-").replace("_", "-")
    v = ALIASES.get(v, v)
    if v not in VERBS:
        raise ValueError(f"verb must be one of {', '.join(VERBS)} "
                         f"(aliases: {', '.join(sorted(ALIASES))})")
    args = v.split("-", 1)
    if execute and v != "arc-status":
        args.append("--execute")
    if force and v == "gpu-wake":
        args.append("--force")
    return args


def fleet_verbs_status() -> str:
    """The fleet as the owner's verbs see it: distro (awnix), systemd state, containers
    running, GPU access (ok | blocked), model posture + gaming drop-in, gaming lock, the
    recorded fleet state, and the two words gpu awake|asleep, fleet awake|asleep|critical.

    Read-only; never boots a stopped distro; answers MAINTENANCE without probing WSL while a
    maintenance restart holds its lock.
    """
    return _run(["status"], timeout=240, rel=_TOOL_REL)


def fleet_verb(verb: str, execute: bool = False, force: bool = False) -> str:
    """Run one of the owner's fleet verbs. DRY RUN unless execute.

    Args:
        verb: gpu-sleep (gaming lock, posture gaming = MicroScheduler lanes to the DGX Spark,
            park every 5090 GPU unit) | gpu-wake (units back one at a time through gpu-boot,
            previous posture, lanes home, lock released; REFUSED when awnix reports GPU access
            blocked -- a maintenance restart is the fix -- or a game is running) | fleet-sleep
            (stop + runtime-mask the fleet, recorded) | fleet-wake (restore the record,
            health-gated, GPU only if awake) | fleet-critical (critical profile + gpu sleep) |
            arc-start | arc-stop | arc-status. Old names alias: gaming, resume, down, up.
        execute: actually run it (default False returns the steps).
        force: gpu-wake only -- wake even while a game runs.

    Returns:
        JSON: verb, ok, dry_run, steps, and refused/error when it did not run.
    """
    try:
        args = verb_argv(verb, execute=execute, force=force)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    key = "-".join(a for a in args[:2] if not a.startswith("--"))
    return _run(args, timeout=_TIMEOUT.get(key, 1800), rel=_TOOL_REL)
