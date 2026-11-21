"""
Local Sandbox Tools -- your own confined container, reachable from your chatbot
===============================================================================

MCP face of ``awnode/sandbox.py`` (``awnode sandbox new|run|ls|stop|rm``).

``local_sandbox_list`` is read-only and always registered. The other three can
start containers and run code, so they follow the node tool-scope rule (shell
only with an explicit opt-in): they are registered ONLY when the user started
``awnode mcp --allow-sandbox`` or set ``sandbox: {allow_mcp: true}`` in
``~/.aither/config.yaml``. Without it they do not exist in tools/list and a
call to them is an unknown tool.

``local_sandbox_exec`` runs inside the confined container only (no network,
all capabilities dropped, read-only root, uid 65534), never on the host. An
agent cannot turn on network egress or delete the user's workspace from here;
those stay CLI choices (``--net``, ``rm`` without ``--keep-work``).
"""

from __future__ import annotations

import json

from awnode import sandbox as _sb

#: Read by awnode.mcp_server: these tools work offline, in every runtime mode.
__scope__ = "shell_local"

#: Read by awnode.mcp_server: tool -> opt-in it needs before it is registered.
__optin__ = {
    "local_sandbox_open": "sandbox",
    "local_sandbox_exec": "sandbox",
    "local_sandbox_close": "sandbox",
}

_MAX_OUTPUT = 100_000


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc),
                       "next_step": getattr(exc, "next_step", ""),
                       "exit_code": getattr(exc, "exit_code", 1)})


def local_sandbox_list() -> str:
    """List this machine's local sandboxes (read-only): id, name, status, engine, workspace, expiry.

    Returns:
        JSON with "sandboxes" (records reconciled against the container engine)
        and "orphans" (labelled containers with no record).
    """
    try:
        out = _sb.listing()
    except _sb.SandboxError as exc:
        return _err(exc)
    out["ok"] = True
    if not out["sandboxes"]:
        out["next_step"] = "awnode sandbox new  (makes a locked-down box on this machine)"
    return json.dumps(out)


def local_sandbox_open(name: str = "") -> str:
    """Make a new locked-down sandbox on this machine (no network, read-only, own /work).

    Args:
        name: Optional short name (a-z, 0-9, - and _). Default t1, t2, ...

    Returns:
        JSON with the sandbox record and the next step.
    """
    try:
        rec = _sb.create(name=name, stream=False)
    except _sb.SandboxError as exc:
        return _err(exc)
    return json.dumps({"ok": True, "sandbox": rec.to_dict(), "you_are_here": _sb.ready_line(rec)})


def local_sandbox_exec(sandbox: str, command: str, timeout: int = 60) -> str:
    """Run a shell command INSIDE a local sandbox (never on the host); returns its output.

    Args:
        sandbox: Sandbox id or name (see local_sandbox_list).
        command: Shell command, run as `sh -c <command>` in /work inside the container.
        timeout: Seconds before the call gives up (1-600, default 60).

    Returns:
        JSON with exit_code, stdout and stderr.
    """
    timeout = min(max(int(timeout or 60), 1), 600)
    try:
        res = _sb.exec_in(sandbox, ["sh", "-c", command], stream=False, timeout=timeout)
    except _sb.SandboxError as exc:
        return _err(exc)
    out = {"ok": res.returncode == 0, "exit_code": res.returncode,
           "stdout": res.stdout[:_MAX_OUTPUT], "stderr": res.stderr[:_MAX_OUTPUT]}
    if len(res.stdout) > _MAX_OUTPUT or len(res.stderr) > _MAX_OUTPUT:
        out["truncated"] = True
    return json.dumps(out)


def local_sandbox_close(sandbox: str) -> str:
    """Remove a local sandbox's container. Its /work folder is kept on disk.

    Args:
        sandbox: Sandbox id or name.

    Returns:
        JSON with what was removed and where the kept workspace is.
    """
    try:
        out = _sb.remove(sandbox, delete_workspace=False)
    except _sb.SandboxError as exc:
        return _err(exc)
    out["ok"] = True
    return json.dumps(out)


__all__ = ["local_sandbox_list", "local_sandbox_open", "local_sandbox_exec",
           "local_sandbox_close"]
