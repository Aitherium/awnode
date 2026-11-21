#!/usr/bin/env python3
"""Policy-proxy stdio<->HTTP bridge to the ``aitheros-rea`` sidecar's MCP endpoint.

The REA toolkit (Node 22, Ghidra, the pinned rea-agents CLI, ~200 tools) runs ONLY
in the policy-gated ``aitheros-rea`` awnix sidecar — never in the hub process. This
bridge is the thin transport the awnode hub starts as the ``rea`` brick: it speaks
stdio MCP to the hub and forwards each request to the sidecar's Streamable-HTTP MCP
endpoint, which serves the rea_* pack whose in-process policy
(``lib.agents.packs.rea.policy.authorize``) is the authoritative gate.

Defence in depth, default-deny and fail-CLOSED: a ``tools/call`` is forwarded only
when its name is one of the four ``rea_*`` pack tools (never a raw upstream tool
name), and a target call (rea_invoke / rea_analyze_javascript) is forwarded only
after the SAME target_class + per-tool grant + argv gate passes here. If the pack
policy is not importable from this hub, or the gate raises, the target call is
REFUSED here — the bridge never forwards an ungated call trusting the sidecar to
catch it (the sidecar re-runs the identical gate as the authoritative second layer).

Stdlib only, Python 3.10+. ``--self-test`` exits 0 pass / 1 fail / 2 cannot-run and
never touches the network.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://aitheros-rea:8791/mcp"
#: The shared bearer the sidecar requires on /mcp (same value on both sides).
TOKEN_ENV = "AITHER_REA_SIDECAR_TOKEN"

#: The ONLY tools/call names this bridge will forward — the rea_* pack door, never a
#: raw upstream tool name (gdb_console, instrument_with_frida, execute_rizin_command,
#: ...). Default-deny: any other name is refused before it leaves the hub, so even if
#: the sidecar MCP ever exposed the ~200 raw tools they cannot be reached from here.
PACK_TOOLS = frozenset({
    "rea_status", "rea_stage_target", "rea_invoke", "rea_analyze_javascript",
    "rea_ledger_close"})

#: The subset of PACK_TOOLS that acts on a target and therefore must pass the
#: target_class + per-tool grant + argv gate before forwarding.
TARGET_CALLS = frozenset({"rea_invoke", "rea_analyze_javascript", "rea_stage_target"})


def _load_policy():
    """Best-effort import of the authoritative pack policy. None when unavailable."""
    try:
        from lib.agents.packs.rea import policy  # type: ignore
        return policy
    except Exception:  # noqa: BLE001 — handled fail-closed by local_gate
        return None


def _call_args(params: dict) -> tuple:
    """(tool_name, target_class, build_args) a rea target tools/call implies.

    rea_invoke carries the inner tool, class and positional args explicitly;
    rea_analyze_javascript fixes the tool name and maps path -> the argv schema."""
    name = (params or {}).get("name") or ""
    args = (params or {}).get("arguments") or {}
    if name == "rea_invoke":
        return (str(args.get("tool") or ""),
                str(args.get("target_class") or ""),
                args.get("args"))
    if name == "rea_stage_target":
        return ("", str(args.get("target_class") or ""), None)
    if name == "rea_analyze_javascript":
        return ("analyze_javascript_application",
                str(args.get("target_class") or ""),
                {"path": str(args.get("path") or "")})
    return "", "", None


def local_gate(policy, method: str, params: dict) -> tuple:
    """(allowed, reason). Default-deny, fail-CLOSED.

    Non-tools/call methods (initialize, tools/list, ping, notifications/*) pass —
    they cannot execute a tool. Every tools/call is gated:

    * a name outside PACK_TOOLS is refused (raw upstream tool names never forward);
    * rea_status / rea_ledger_close (no target) pass;
    * a target call (rea_invoke / rea_analyze_javascript) is refused unless the pack
      policy is importable AND both authorize() and build_argv() pass. If the policy
      cannot be imported, or authorize()/build_argv() raise, the call is REFUSED —
      the bridge never forwards an ungated target call on the assumption the sidecar
      will catch it.
    """
    if method != "tools/call":
        return True, ""
    name = ((params or {}).get("name") or "").strip()
    if name not in PACK_TOOLS:
        return False, ("tool %r is not a permitted rea_* pack tool; only %s may be "
                       "called (default-deny)" % (name, ", ".join(sorted(PACK_TOOLS))))
    if name not in TARGET_CALLS:
        return True, ""
    if policy is None:
        return False, ("rea policy is not importable in this hub; target calls are "
                       "refused here (fail-closed) — the sidecar is reached only "
                       "through a gate")
    tool_name, target_class, build_args = _call_args(params)
    try:
        if name == "rea_stage_target":
            # No tool runs; the class gate alone (the sidecar re-checks it).
            return policy.authorize_class(target_class)
        ok, reason = policy.authorize(tool_name, target_class)
        if ok:
            ok, reason, _argv = policy.build_argv(tool_name, build_args)
    except Exception as exc:  # noqa: BLE001 — a policy fault denies, never allows
        return False, "rea policy error (fail-closed): %s" % exc
    return ok, reason


def _refusal(req_id, reason: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"isError": True,
                       "content": [{"type": "text", "text": "REA policy refusal: " + reason}]}}


def forward(url: str, message: dict, timeout: float = 960.0) -> dict:
    """POST one JSON-RPC message to the sidecar over https with the shared bearer.
    Raises ValueError (handled by serve) when the URL is plaintext or no bearer is set."""
    if not url.startswith("https://"):
        raise ValueError("refusing plaintext sidecar URL %r (the fleet speaks TLS)" % url)
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ValueError("%s is not set; the sidecar refuses unauthenticated calls" % TOKEN_ENV)
    body = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": "Bearer " + token})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 — fixed internal URL
        return json.loads(resp.read().decode("utf-8") or "{}")


def serve(url: str, policy=None, stdin=None, stdout=None) -> int:
    policy = policy if policy is not None else _load_policy()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method", "")
        params = msg.get("params") or {}
        req_id = msg.get("id")
        allowed, reason = local_gate(policy, method, params)
        if not allowed:
            stdout.write(json.dumps(_refusal(req_id, reason)) + "\n")
            stdout.flush()
            continue
        try:
            reply = forward(url, msg)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if req_id is None:
                continue
            reply = {"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": -32011, "message": "rea sidecar unreachable: %s" % exc}}
        if req_id is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0


class _FakePolicy:
    @staticmethod
    def authorize_class(target_class):
        return (True, "") if target_class else (False, "target_class required")

    @staticmethod
    def authorize(tool_name, target_class, granted=None):
        if not target_class:
            return False, "target_class required"
        if tool_name == "gdb_console":
            return False, "gdb_console requires an explicit grant"
        return True, ""

    @staticmethod
    def build_argv(tool_name, args):
        # Accept exactly {"path": <abs>} for the read-only tools used in the test.
        if not isinstance(args, dict) or set(args) != {"path"}:
            return False, "args must be {path}", []
        if not str(args["path"]).startswith("/"):
            return False, "path must be absolute", []
        return True, "", [tool_name, args["path"], "--json"]


class _RaisingPolicy:
    @staticmethod
    def authorize(tool_name, target_class, granted=None):
        raise RuntimeError("policy backend down")

    @staticmethod
    def build_argv(tool_name, args):
        raise RuntimeError("policy backend down")


def _self_test() -> int:
    pol = _FakePolicy()
    args_ok = {"path": "/targets/app.bin"}
    # 1. missing target_class on rea_invoke is refused locally.
    ok, _ = local_gate(pol, "tools/call",
                       {"name": "rea_invoke",
                        "arguments": {"tool": "inspect_go_binary", "args": args_ok}})
    if ok:
        print("FAIL: rea_invoke without target_class should be refused")
        return 1
    # 2. a grant-required tool is refused even with a class.
    ok, _ = local_gate(pol, "tools/call",
                       {"name": "rea_invoke",
                        "arguments": {"tool": "gdb_console", "target_class": "owned",
                                      "args": args_ok}})
    if ok:
        print("FAIL: gdb_console should be refused without a grant")
        return 1
    # 3. a read-only tool with a class AND a valid argv passes.
    ok, reason = local_gate(pol, "tools/call",
                            {"name": "rea_invoke",
                             "arguments": {"tool": "inspect_go_binary",
                                           "target_class": "owned", "args": args_ok}})
    if not ok:
        print("FAIL: inspect_go_binary with a class should pass: %s" % reason)
        return 1
    # 4. a bad argv (relative path) is refused even when authorize passes.
    ok, _ = local_gate(pol, "tools/call",
                       {"name": "rea_invoke",
                        "arguments": {"tool": "inspect_go_binary", "target_class": "owned",
                                      "args": {"path": "etc/passwd"}}})
    if ok:
        print("FAIL: a bad argv must be refused")
        return 1
    # 5. a raw upstream tool name as the tools/call name is default-denied.
    ok, _ = local_gate(pol, "tools/call",
                       {"name": "gdb_console", "arguments": {"target_class": "owned"}})
    if ok:
        print("FAIL: a non-pack tools/call name must be refused")
        return 1
    # 6. FAIL-CLOSED: no policy -> a target call is refused (not forwarded).
    ok, _ = local_gate(None, "tools/call",
                       {"name": "rea_invoke",
                        "arguments": {"tool": "inspect_go_binary", "target_class": "owned",
                                      "args": args_ok}})
    if ok:
        print("FAIL: with no policy a target call must be refused (fail-closed)")
        return 1
    # 7. FAIL-CLOSED: a policy that raises refuses the call.
    ok, _ = local_gate(_RaisingPolicy(), "tools/call",
                       {"name": "rea_invoke",
                        "arguments": {"tool": "inspect_go_binary", "target_class": "owned",
                                      "args": args_ok}})
    if ok:
        print("FAIL: a raising policy must refuse (fail-closed)")
        return 1
    # 8. a non-target pack tool passes even with no policy.
    ok, _ = local_gate(None, "tools/call", {"name": "rea_status", "arguments": {}})
    if not ok:
        print("FAIL: rea_status must pass")
        return 1
    # 9. non-tools/call methods pass untouched.
    if not local_gate(pol, "tools/list", {})[0]:
        print("FAIL: tools/list must not be gated")
        return 1
    # 10. a refusal is well-formed MCP.
    r = _refusal("x", "nope")
    if not r["result"]["isError"] or r["id"] != "x":
        print("FAIL: refusal shape")
        return 1
    print("ok: rea_sidecar_bridge self-test passed")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL, help="sidecar MCP endpoint")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return _self_test()
    return serve(args.url)


if __name__ == "__main__":
    sys.exit(main())
