"""
AwNode CLI
===============

    awnode start               # Start on port 8090 (auto-detect backends)
    awnode start --cloud       # Force cloud mode
    awnode start --port 9090   # Custom port
    awnode status              # Show backend status
    awnode connect <api_key>   # Register with Elysium
    awnode setup               # Detect GPU, pull models, configure
    awnode mcp                 # Start MCP server (stdio, for Claude Code)
    awnode mcp --transport sse # Start MCP server (HTTP SSE)
    awnode mcp-config          # Output .mcp.json config snippet
    awnode deploy <service>    # Deploy via AitherComet
"""

import argparse
import asyncio
import json
import os
import sys


def main():
    # License entitlement gate (logs tier, wires metering quotas; never blocks unless
    # AITHER_LICENSE_REQUIRE=1). adk must be importable for this to apply.
    try:
        from adk.license_startup import gate_startup
        gate_startup(product="node")
    except Exception:
        pass  # adk/licensing unavailable — node runs unmetered (dev)

    parser = argparse.ArgumentParser(
        prog="awnode",
        description="AwNode — lightweight local gateway for AitherOS",
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # start
    start_p = sub.add_parser("start", help="Start AwNode server")
    start_p.add_argument("--port", type=int, default=8090, help="Port (default: 8090)")
    start_p.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1; set to 0.0.0.0 to listen on all interfaces with bearer token auth)")
    start_p.add_argument("--cloud", action="store_true", help="Force cloud mode (Elysium)")
    start_p.add_argument("--local", action="store_true", help="Force local mode (no cloud)")
    start_p.add_argument("--vllm-url", type=str, help="vLLM URL (default: http://localhost:8120)")
    start_p.add_argument("--ollama-url", type=str, help="Ollama URL (default: http://localhost:11434)")
    start_p.add_argument("--genesis-url", type=str, help="Genesis URL (default: http://localhost:8001)")

    # status
    sub.add_parser("status", help="Show backend status")

    # connect
    connect_p = sub.add_parser("connect", help="Register with Elysium cloud")
    connect_p.add_argument("api_key", nargs="?", help="API key (or set AITHER_API_KEY)")
    connect_p.add_argument(
        "--enroll-token", dest="enroll_token", help="Short-lived enrollment token"
    )
    connect_p.add_argument(
        "--portal", dest="portal_url", help="Portal URL override"
    )

    # login — browser device flow (RFC 8628) against AitherIdentity.
    # Delegates to adk's proven login so a standalone node binary (which bundles
    # adk as a dependency) can authenticate with NO separate adk install.
    login_p = sub.add_parser("login", help="Authenticate with Aitherium (browser device flow)")
    login_p.add_argument("--api-key", dest="api_key", help="Skip the browser — save this API key directly")
    login_p.add_argument("--email", help="Email/password login instead of device flow")
    login_p.add_argument("--password", help="Password (prompted if omitted with --email)")
    login_p.add_argument("--portal-url", dest="portal_url", help="Identity/portal base URL override")

    # setup
    setup_p = sub.add_parser("setup", help="Detect GPU, pull models, configure backends")
    setup_p.add_argument(
        "target", nargs="?", default=None,
        help="Optional setup target: 'comfyui' to self-host ComfyUI and join the "
             "AitherOS fabric. Omit for the default GPU/backend setup.",
    )
    setup_p.add_argument("--cloud", action="store_true", help="Cloud-only mode (skip local GPU)")
    setup_p.add_argument("--model", type=str, help="Specific Ollama model to pull")
    setup_p.add_argument("--skip-model", action="store_true", help="Skip model download")
    # `setup comfyui` flags (ignored by the bare `setup` path)
    setup_p.add_argument("--profile", type=str, default="studio",
                         help="[comfyui] Model profile from comfyui-model-profiles.yaml (default: studio)")
    setup_p.add_argument("--workspace", "--workspace-id", dest="workspace_id", type=str, default=None,
                         help="[comfyui] Bind to this workspace (else from connect state)")
    setup_p.add_argument("--tenant", type=str, default=None,
                         help="[comfyui] RBAC tenant (else from connect state)")
    setup_p.add_argument("--mesh-url", dest="mesh_url", type=str, default=None,
                         help="[comfyui] AitherMesh base (default: env AITHERMESH_URL/AITHER_MESH_URL)")
    setup_p.add_argument("--port", type=int, default=8188,
                         help="[comfyui] ComfyUI port (default: 8188)")
    setup_p.add_argument("--skip-models", action="store_true",
                         help="[comfyui] Join + bind only, no model pull")
    setup_p.add_argument("--dry-run", action="store_true",
                         help="[comfyui] Plan only — no install/network mutation")

    # mcp
    mcp_p = sub.add_parser("mcp", help="Start MCP server (for Claude Code, Cursor)")
    mcp_p.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                        help="Transport (default: stdio)")
    mcp_p.add_argument("--host", default="127.0.0.1", help="SSE bind host (default: 127.0.0.1; set to 0.0.0.0 for all interfaces)")
    mcp_p.add_argument("--port", type=int, default=8090, help="SSE port")

    # mcp-config
    sub.add_parser("mcp-config", help="Output .mcp.json config for Claude Code / Cursor")

    # ext (extensions)
    ext_p = sub.add_parser("ext", help="Manage extensions (Canvas, ComfyUI, agents)")
    ext_sub = ext_p.add_subparsers(dest="ext_command")
    ext_sub.add_parser("list", help="List available extensions")
    ext_install_p = ext_sub.add_parser("install", help="Install an extension")
    ext_install_p.add_argument("extension_id", help="Extension ID (e.g., canvas, comfyui)")
    ext_start_p = ext_sub.add_parser("start", help="Start an extension")
    ext_start_p.add_argument("extension_id", help="Extension ID")
    ext_stop_p = ext_sub.add_parser("stop", help="Stop an extension")
    ext_stop_p.add_argument("extension_id", help="Extension ID")
    ext_sub.add_parser("status", help="Check all extension status")

    # deploy
    deploy_p = sub.add_parser("deploy", help="Deploy a service via AitherComet")
    deploy_p.add_argument("service", help="Service name to deploy")
    deploy_p.add_argument("--target", default="docker", choices=["docker", "docker-compose", "kubernetes", "systemd", "podman", "cloud-gpu"])
    deploy_p.add_argument("--strategy", default="rolling", choices=["rolling", "blue-green", "canary", "recreate"])

    # service — persistent host service (Scheduled Task / systemd --user / launchd)
    svc_p = sub.add_parser(
        "service",
        help="Install/manage AwNode as a persistent host service "
             "(Windows Scheduled Task, systemd user unit, or launchd agent)",
    )
    svc_p.add_argument("action", choices=["install", "uninstall", "status", "start", "stop"])
    svc_p.add_argument("--port", type=int, default=8090, help="Port the service binds (default: 8090)")
    svc_p.add_argument(
        "--service-host", dest="service_host", default=None,
        help="Bind host for the installed service (default: 127.0.0.1 — loopback only; "
             "set 0.0.0.0 to expose on LAN, which also requires AITHERNODE_PROXY_TOKEN "
             "for the /proxy plane)",
    )

    # doctor
    doctor_p = sub.add_parser("doctor", help="End-to-end health check (license, portal, backends)")
    doctor_p.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output")
    doctor_p.add_argument("--timeout", type=float, default=3.0, help="Per-check HTTP timeout (default: 3s)")

    args = parser.parse_args()

    if args.command == "start":
        if args.cloud:
            os.environ["AITHERNODE_MODE"] = "cloud"
        elif args.local:
            os.environ["AITHERNODE_MODE"] = "local"
        if args.vllm_url:
            os.environ["AITHER_VLLM_URL"] = args.vllm_url
        if args.ollama_url:
            os.environ["OLLAMA_HOST"] = args.ollama_url
        if args.genesis_url:
            os.environ["AITHER_URL"] = args.genesis_url
        os.environ["AITHERNODE_PORT"] = str(args.port)

        import uvicorn
        uvicorn.run(
            "awnode.server:app",
            host=args.host,
            port=args.port,
            log_level="info",
        )

    elif args.command == "status":
        asyncio.run(_status())

    elif args.command == "connect":
        api_key = args.api_key or os.environ.get("AITHER_API_KEY", "")
        enroll_token = getattr(args, "enroll_token", None)
        portal_url = getattr(args, "portal_url", None)

        if not api_key and not enroll_token:
            print("Error: provide API key or --enroll-token")
            sys.exit(1)

        asyncio.run(_connect(
            api_key=api_key,
            enroll_token=enroll_token,
            portal_url=portal_url,
        ))

    elif args.command == "login":
        sys.exit(_login(args))

    elif args.command == "setup" and getattr(args, "target", None) == "comfyui":
        from awnode.comfyui_setup import _setup_comfyui
        sys.exit(asyncio.run(_setup_comfyui(
            profile=args.profile,
            workspace_id=args.workspace_id,
            tenant=args.tenant,
            mesh_url=args.mesh_url,
            port=args.port,
            skip_models=args.skip_models,
            dry_run=args.dry_run,
        )))

    elif args.command == "setup":
        if getattr(args, "target", None):
            print(f"Unknown setup target: {args.target!r}. "
                  "Use 'comfyui' or omit for default backend setup.")
            sys.exit(2)
        asyncio.run(_setup(
            cloud_only=args.cloud,
            model=args.model,
            skip_model=args.skip_model,
        ))

    elif args.command == "mcp":
        from awnode.mcp_server import main as mcp_main
        # Forward args to mcp_server's argparse
        sys.argv = ["awnode-mcp",
                     "--transport", args.transport,
                     "--host", args.host,
                     "--port", str(args.port)]
        mcp_main()

    elif args.command == "mcp-config":
        _mcp_config()

    elif args.command == "ext":
        asyncio.run(_ext(args))

    elif args.command == "deploy":
        asyncio.run(_deploy(args.service, args.target, args.strategy))

    elif args.command == "doctor":
        sys.exit(asyncio.run(_doctor(emit_json=args.json, timeout=args.timeout)))

    elif args.command == "service":
        from awnode.service_install import service_action
        try:
            result = service_action(args.action, port=args.port, host=args.service_host)
            print(f"[service] {result}")
        except (RuntimeError, ValueError, OSError) as e:
            print(f"[service] ERROR: {e}")
            sys.exit(1)

    else:
        parser.print_help()


async def _doctor(*, emit_json: bool = False, timeout: float = 3.0) -> int:
    """End-to-end health check.

    Returns exit code: 0 if everything healthy, 1 if any required surface
    is degraded, 2 if hard failure (no license + no offline grace).
    """
    import httpx
    from pathlib import Path

    aither_home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    license_cache = aither_home / "license.json"
    portal_token = aither_home / "portal.token"

    report: dict = {
        "aither_home": str(aither_home),
        "checks": {},
        "summary": {"ok": 0, "warn": 0, "fail": 0},
    }

    def record(name: str, status: str, detail: str = "", **extra) -> None:
        report["checks"][name] = {"status": status, "detail": detail, **extra}
        report["summary"][status] = report["summary"].get(status, 0) + 1

    # 1. AITHER_HOME directory
    if aither_home.exists():
        record("aither_home", "ok", f"{aither_home}")
    else:
        record("aither_home", "warn", "missing — will be created on first run")

    # 2. License cache + entitlement
    if license_cache.exists():
        try:
            ent = json.loads(license_cache.read_text())
            plan = ent.get("plan", "?")
            expires = ent.get("expires_at", "?")
            record("license_cache", "ok", f"plan={plan} expires={expires}", plan=plan)
        except Exception as e:
            record("license_cache", "fail", f"unreadable: {e}")
    else:
        record("license_cache", "warn", "no cached entitlement — free tier in effect")

    # 3. API key env
    api_key = os.environ.get("AITHER_API_KEY", "")
    if api_key:
        masked = api_key[:6] + "…" + api_key[-4:] if len(api_key) > 12 else "set"
        record("api_key", "ok", f"AITHER_API_KEY={masked}")
    else:
        record("api_key", "warn", "AITHER_API_KEY not set — paid features will be locked")

    # 4. Portal token (from `aithershell login` / `aither login`)
    if portal_token.exists():
        try:
            size = portal_token.stat().st_size
            record("portal_token", "ok", f"{portal_token} ({size}B)")
        except Exception as e:
            record("portal_token", "warn", str(e))
    else:
        record("portal_token", "warn", "no portal token — run `aithershell login`")

    # 5. Backend reachability
    backends = {
        "genesis": os.environ.get("AITHER_URL", "http://localhost:8001"),
        "vllm": os.environ.get("AITHER_VLLM_URL", "http://localhost:8120"),
        "ollama": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        "gateway": os.environ.get("AITHER_CLOUD_URL", "https://gateway.aitherium.com"),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        for name, url in backends.items():
            health_path = "/api/tags" if name == "ollama" else "/health"
            try:
                r = await client.get(url.rstrip("/") + health_path)
                if r.status_code < 500:
                    record(name, "ok", f"{url} ({r.status_code})")
                else:
                    record(name, "warn", f"{url} -> {r.status_code}")
            except Exception as e:
                # gateway/vllm/ollama unreachable is a warn (not everyone runs them)
                record(name, "warn", f"{url} unreachable: {type(e).__name__}")

    # Compute exit code
    fail = report["summary"].get("fail", 0)
    warn = report["summary"].get("warn", 0)
    # Hard fail only if license cache is corrupt; otherwise warns are OK
    exit_code = 2 if fail else (1 if warn > 4 else 0)

    if emit_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("AwNode Doctor")
        print("=" * 60)
        print(f"AITHER_HOME: {aither_home}")
        print()
        glyph = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}
        for name, info in report["checks"].items():
            print(f"  {glyph.get(info['status'], '[?]   ')} {name:<16} {info['detail']}")
        print()
        s = report["summary"]
        print(f"Summary: {s.get('ok',0)} ok, {s.get('warn',0)} warn, {s.get('fail',0)} fail")
        if exit_code == 0:
            print("Status: healthy")
        elif exit_code == 1:
            print("Status: degraded (paid features may be unavailable)")
        else:
            print("Status: FAIL — license cache or required backend broken")

    return exit_code


async def _status():
    """Check backend availability."""
    import httpx

    checks = {
        "Genesis": os.environ.get("AITHER_URL", "http://localhost:8001"),
        "vLLM": os.environ.get("AITHER_VLLM_URL", os.environ.get("VLLM_URL", "http://localhost:8120")),
        "Ollama": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        "Elysium": "https://mcp.aitherium.com",
    }

    print("AwNode Backend Status")
    print("=" * 40)
    for name, url in checks.items():
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                health_path = "/api/tags" if name == "Ollama" else "/health"
                r = await c.get(f"{url.rstrip('/')}{health_path}")
                status = "UP" if r.status_code == 200 else f"HTTP {r.status_code}"
        except Exception:
            status = "DOWN"
        icon = "+" if status == "UP" else "-"
        print(f"  [{icon}] {name:12s} {url:40s} {status}")

    # Check for running Node
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get("http://localhost:8090/status")
            if r.status_code == 200:
                data = r.json()
                print(f"\nNode running on port {data.get('port', 8090)} — mode: {data.get('mode', '?')}")
    except Exception:
        print("\nNode not running. Start with: awnode start")


_DEFAULT_IDENTITY_URL = "https://idp.aitherium.com"


def _login(args) -> int:
    """Authenticate — API key, or browser device flow (RFC 8628).

    Primary path delegates to adk's richer ``cmd_login`` (device / email / key
    + endpoint persistence). If adk isn't importable (e.g. a stripped standalone
    binary), we fall back to a self-contained stdlib device flow so login ALWAYS
    works with zero extra dependencies. Either way credentials land in the shared
    ``~/.aither/auth.json`` that ``awnode connect`` and adk-backed tools read.
    """
    # Fast path: a directly-provided API key needs no network round-trip.
    api_key = getattr(args, "api_key", None)
    identity_url = (
        getattr(args, "portal_url", None)
        or os.environ.get("AITHER_PORTAL_URL")
        or os.environ.get("AITHERIDENTITY_URL")
        or _DEFAULT_IDENTITY_URL
    ).rstrip("/")

    # Primary: reuse adk's richer login if adk is installed. Dynamic import so a
    # stripped standalone binary (adk not bundled) simply falls through to the
    # self-contained flow below instead of dragging the whole adk monolith.
    try:
        import importlib
        cmd_login = importlib.import_module("adk.cli").cmd_login
    except Exception:  # noqa: BLE001 — ImportError, AttributeError, or a broken adk env
        cmd_login = None
    if cmd_login is not None:
        from argparse import Namespace
        ns = Namespace(
            portal_url=getattr(args, "portal_url", None),
            api_key=api_key,
            email=getattr(args, "email", None),
            password=getattr(args, "password", None),
        )
        try:
            return int(cmd_login(ns) or 0)
        except SystemExit as e:  # cmd_login may sys.exit on some paths
            return int(e.code or 0)

    # Fallback: self-contained.
    if getattr(args, "email", None):
        print("  Email/password login needs the adk package. Install: pip install awdk")
        return 1
    if api_key:
        _save_auth_json(identity_url, {"access_token": api_key, "token_type": "api_key"})
        print("  API key saved to ~/.aither/auth.json")
        return 0
    try:
        token = _device_flow_standalone(identity_url)
    except RuntimeError as exc:
        print(f"  Error: {exc}")
        return 1
    _save_auth_json(identity_url, token)
    print("  Signed in. Credentials saved to ~/.aither/auth.json")
    return 0


def _device_flow_standalone(identity_url: str) -> dict:
    """Minimal RFC-8628 device-code flow using only the stdlib.

    Hits the same AitherIdentity endpoints as adk (``/auth/device/code`` +
    ``/auth/device/token``) so a standalone binary authenticates identically.
    """
    import json as _json
    import time
    import urllib.error
    import urllib.request
    import webbrowser

    ua = "awnode/login"

    def _post(path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{identity_url}{path}",
            data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": ua},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read())

    try:
        data = _post("/auth/device/code", {"client_name": "awnode", "scopes": "full"})
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Cannot reach {identity_url}: {exc}") from exc

    user_code = data["user_code"]
    device_code = data["device_code"]
    verify = data.get("verification_uri_complete") or data.get("verification_uri", "")
    interval = max(2, int(data.get("interval", 5)))
    expires_in = int(data.get("expires_in", 900))

    print()
    print(f"  Your code: {user_code}")
    print(f"  Opening browser to: {verify}")
    print("  (If it doesn't open, visit the URL and enter the code.)")
    try:
        webbrowser.open(verify)
    except OSError:
        pass
    print("  Waiting for approval", end="", flush=True)

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        try:
            result = _post("/auth/device/token", {"device_code": device_code})
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                try:
                    detail = _json.loads(exc.read()).get("detail", "")
                except (ValueError, OSError):
                    detail = ""
                if detail in ("expired_token", "invalid_device_code"):
                    print()
                    raise RuntimeError(f"{detail}. Run `awnode login` again.") from exc
            print(".", end="", flush=True)
            continue
        except (urllib.error.URLError, OSError):
            print(".", end="", flush=True)
            continue
        if result.get("status") == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if result.get("access_token"):
            print(" approved!")
            return result
        print(".", end="", flush=True)

    print()
    raise RuntimeError("Timed out waiting for approval. Run `awnode login` again.")


def _save_auth_json(endpoint: str, token: dict) -> None:
    """Persist credentials to ~/.aither/auth.json in the shared adk/shell format."""
    import stat
    from pathlib import Path

    path = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "endpoint": endpoint,
        "token_type": token.get("token_type", "bearer"),
        "access_token": token.get("access_token", ""),
        "expires_at": token.get("expires_at", ""),
        "user": token.get("user", {}),
    }
    store = {"version": 1, "active_profile": "portal", "profiles": {"portal": profile}}
    path.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


async def _connect(
    api_key: str = "",
    enroll_token: str = "",
    portal_url: str = "",
):
    """Register with Elysium or enroll via token."""
    import httpx
    import platform
    from pathlib import Path

    # Resolve portal URL
    if not portal_url:
        portal_url = os.environ.get(
            "AITHER_CLOUD_URL", "https://portal.aitherium.com"
        )

    aither_home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    state_file = aither_home / "sync_state.json"

    # Path 1: Exchange enrollment token for workspace-scoped token
    if enroll_token:
        print("Exchanging enrollment token for workspace token...")
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.post(
                    f"{portal_url}/v1/workspace/api-keys/enrollment-token/exchange",
                    json={"enrollment_token": enroll_token},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    workspace_token = data.get("token", "")
                    node_id = data.get("node_id", "")
                    if workspace_token:
                        # Save token to local state
                        import json
                        aither_home.mkdir(parents=True, exist_ok=True)
                        state = {}
                        if state_file.exists():
                            try:
                                state = json.loads(state_file.read_text())
                            except Exception:
                                pass
                        state["tenant_scoped_token"] = workspace_token
                        state["node_id"] = node_id
                        state_file.write_text(json.dumps(state, indent=2))
                        print(
                            f"✓ Token saved for node {node_id} "
                            f"(expires {data.get('expires_at', '?')})"
                        )
                        print("Start sync with: awnode start")
                        return
                elif resp.status_code == 401:
                    print("Error: Invalid or expired enrollment token")
                else:
                    print(
                        f"Error: Token exchange failed "
                        f"({resp.status_code}): {resp.text[:200]}"
                    )
        except Exception as e:
            print(f"Error exchanging enrollment token: {e}")
        sys.exit(1)

    # Path 2: Traditional API key registration
    if api_key:
        from adk.client import GatewayClient

        gw = GatewayClient(api_key=api_key)
        hostname = platform.node() or "local"

        try:
            result = await gw.register_agent(
                name=f"node-{hostname}",
                capabilities=["chat", "code", "filesystem", "git", "vllm", "ollama"],
                description=f"AwNode on {hostname} ({platform.system()})",
            )
            print(f"Connected! Agent ID: {result.get('agent_id', '?')}")
            print(f"Save your API key: export AITHER_API_KEY={api_key}")

            # Save to config
            try:
                from adk.shell.config import CONFIG_FILE
                import yaml

                cfg = {}
                if CONFIG_FILE.exists():
                    with open(CONFIG_FILE) as f:
                        cfg = yaml.safe_load(f) or {}
                cfg["api_key"] = api_key
                with open(CONFIG_FILE, "w") as f:
                    yaml.dump(cfg, f)
                print(f"Saved to {CONFIG_FILE}")
            except Exception:
                pass
        except Exception as e:
            print(f"Connection failed: {e}")
            sys.exit(1)


async def _deploy(service: str, target: str, strategy: str):
    """Deploy via AitherComet."""
    import httpx

    genesis_url = os.environ.get("AITHER_URL", "http://localhost:8001")
    api_key = os.environ.get("AITHER_API_KEY", "")

    print(f"Deploying {service} → {target} ({strategy})...")
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = await c.post(f"{genesis_url}/deploy/deploy", json={
                "service": service,
                "target": target,
                "strategy": strategy,
            }, headers=headers)
            data = r.json()
            if r.status_code == 200:
                print(f"Deployment started: {data.get('deployment_id', '?')}")
                print(f"Status: {data.get('status', '?')}")
            else:
                print(f"Error: {data}")
    except Exception as e:
        print(f"Deploy failed: {e}")
        print("Is Genesis running? Start AitherOS or connect to cloud.")
        sys.exit(1)


async def _setup(cloud_only: bool = False, model: str = None, skip_model: bool = False):
    """Detect hardware, configure backends, optionally pull models."""
    import shutil
    import subprocess
    from pathlib import Path

    import yaml

    config_dir = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    config_file = config_dir / "config.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)

    cfg = {}
    if config_file.exists():
        with open(config_file) as f:
            cfg = yaml.safe_load(f) or {}

    print("AwNode Setup")
    print("=" * 40)

    # -- GPU Detection --
    gpu_vram = 0
    gpu_name = ""
    if not cloud_only:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                line = result.stdout.strip().split("\n")[0]
                parts = line.rsplit(",", 1)
                gpu_name = parts[0].strip()
                gpu_vram = int(parts[1].strip()) if len(parts) > 1 else 0
                print(f"  GPU: {gpu_name} ({gpu_vram} MB VRAM)")
            else:
                print("  GPU: Not detected (nvidia-smi failed)")
        except FileNotFoundError:
            print("  GPU: Not detected (nvidia-smi not found)")
        except Exception as e:
            print(f"  GPU: Detection error ({e})")

    cfg["gpu"] = {"name": gpu_name, "vram_mb": gpu_vram}

    # -- Backend Selection --
    if cloud_only:
        cfg["mode"] = "cloud"
        print("  Mode: cloud-only (Elysium)")
        api_key = cfg.get("api_key") or os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            print("\n  Set your API key: awnode connect <your-api-key>")
    elif gpu_vram >= 16384:
        cfg["mode"] = "auto"
        print(f"  Recommended: vLLM (GPU has {gpu_vram}MB VRAM)")
        print("  Local models will use your GPU for fast inference.")
    elif gpu_vram >= 4096:
        cfg["mode"] = "auto"
        print(f"  Recommended: Ollama with small models ({gpu_vram}MB VRAM)")
    else:
        cfg["mode"] = "auto"
        print("  Recommended: Ollama CPU mode or cloud")

    # -- Ollama Detection & Model Pull --
    has_ollama = shutil.which("ollama") is not None
    if has_ollama:
        print("  Ollama: installed")
        cfg["ollama"] = {"available": True}

        if not skip_model and not cloud_only:
            target_model = model
            if not target_model:
                if gpu_vram >= 16384:
                    target_model = "llama3.1:8b"
                elif gpu_vram >= 8192:
                    target_model = "llama3.2:3b"
                else:
                    target_model = "llama3.2:1b"

            print(f"\n  Pulling model: {target_model}...")
            pull_result = subprocess.run(
                ["ollama", "pull", target_model],
                timeout=600,
            )
            if pull_result.returncode == 0:
                cfg["ollama"]["default_model"] = target_model
                print(f"  Model ready: {target_model}")
            else:
                print(f"  Model pull failed (exit {pull_result.returncode})")
    else:
        print("  Ollama: not installed")
        if not cloud_only:
            print("    Install: https://ollama.com/download")
        cfg["ollama"] = {"available": False}

    # -- vLLM Detection --
    import httpx
    vllm_url = os.environ.get("AITHER_VLLM_URL", "http://localhost:8120")
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{vllm_url}/health")
            if r.status_code == 200:
                print(f"  vLLM: running at {vllm_url}")
                cfg["vllm"] = {"available": True, "url": vllm_url}
            else:
                cfg["vllm"] = {"available": False}
    except Exception:
        cfg["vllm"] = {"available": False}

    # -- Save config --
    with open(config_file, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)
    print(f"\n  Config saved: {config_file}")

    print("\n  Quick start:")
    print("    awnode start           # Start local gateway")
    print("    awnode mcp             # Start MCP server (for Claude Code)")
    print("    awnode mcp-config      # Get .mcp.json config")


def _mcp_config():
    """Output MCP client configuration for Claude Code / Cursor."""
    import shutil

    awnode_path = shutil.which("awnode") or "awnode"

    config = {
        "mcpServers": {
            "awnode": {
                "command": awnode_path,
                "args": ["mcp"],
                "env": {},
            }
        }
    }

    api_key = os.environ.get("AITHER_API_KEY", "")
    if api_key:
        config["mcpServers"]["awnode"]["env"]["AITHER_API_KEY"] = api_key

    print(json.dumps(config, indent=2))
    print("\nAdd the above to your .mcp.json (Claude Code) or MCP settings (Cursor).")
    print("Or copy just the 'awnode' block into an existing .mcp.json.")


async def _ext(args):
    """Manage extensions: list, install, start, stop, status."""
    from awnode.extensions import get_extension_manager
    mgr = get_extension_manager()

    if args.ext_command == "list":
        catalog = mgr.list_catalog()
        print(
            f"{'ID':15s} {'Name':20s} {'Category':10s} "
            f"{'Status':10s} {'GPU':5s} Description"
        )
        print("-" * 100)
        for ext in catalog:
            gpu = "Yes" if ext.get("gpu") else "No"
            print(
                f"{ext['id']:15s} {ext['name']:20s} "
                f"{ext.get('category', ''):10s} "
                f"{ext.get('status', 'available'):10s} "
                f"{gpu:5s} {ext['description'][:50]}"
            )

    elif args.ext_command == "install":
        result = await mgr.install(args.extension_id)
        if "error" in result:
            print(f"Error: {result['error']}")
            if result.get("hint"):
                print(f"Hint: {result['hint']}")
        else:
            print(f"Installed: {args.extension_id}")

    elif args.ext_command == "start":
        result = await mgr.start(args.extension_id)
        if "error" in result:
            print(f"Error: {result['error']}")
            if result.get("hint"):
                print(f"Hint: {result['hint']}")
        else:
            print(f"Started: {args.extension_id}")
            print(f"  URL: {result.get('url', '?')}")
            if result.get("tools"):
                print(f"  Tools: {', '.join(result['tools'])}")

    elif args.ext_command == "stop":
        result = await mgr.stop(args.extension_id)
        if "error" not in result:
            print(f"Stopped: {args.extension_id}")
        else:
            print(f"Error: {result['error']}")

    elif args.ext_command == "status":
        results = await mgr.health_check_all()
        if not results:
            print("No extensions running.")
        for ext_id, status in results.items():
            icon = "+" if status.get("status") == "running" else "-"
            print(
                f"  [{icon}] {ext_id:15s} "
                f"{status.get('url', ''):30s} "
                f"{status.get('status', '?')}"
            )

    else:
        print("Usage: awnode ext [list|install|start|stop|status]")


if __name__ == "__main__":
    main()
