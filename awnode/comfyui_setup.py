"""
Self-host ComfyUI deploy kit (Pillar 6, slice 3)
=================================================

`aither setup comfyui` orchestration. Lets a customer stand up their OWN
ComfyUI on their box, join it to the AitherOS fabric, pull a model profile, and
bind it to THEIR workspace. We ASSIST the deploy + workspace-connect; the
customer runs it.

This module is deliberately thin glue over what already exists:

  INSTALL/START    awnode.extensions.ExtensionManager (the `comfyui`
                   ExtensionManifest — image aitherium/aither-comfyui:latest,
                   port 8188). We never shell out to docker/git directly.
  MODELS           AitherOS lib.compute.comfyui_models (load_profile/to_downloads)
                   when importable; else we read comfyui-model-profiles.yaml
                   directly. The resolved AITHER_MODEL_DOWNLOADS JSON is what the
                   Dockerfile.comfyui-cloud entrypoint already consumes — we inject
                   it into the container env at start.
  MESH-JOIN        AitherOS lib.compute.comfyui_node.join_fabric (PSK mesh join +
                   Compute/Scheduler register). Guarded — degrades gracefully if
                   the AitherOS lib is not on the box (workspace bind still works).
  WORKSPACE-BIND   awnode.sync.WorkspaceSyncClient.register_endpoint() /
                   sync_workspace() — the node becomes owned by the customer's
                   workspace.

NOTE: no `from __future__ import annotations` here (adk schema-inference rule).
Every side-effecting step is gated behind `dry_run` so `--dry-run` plans only.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

COMFYUI_EXT_ID = "comfyui"
DEFAULT_PROFILE = "studio"
FABRIC_ROLE = "image_generation"


# ---------------------------------------------------------------------------
# AitherOS lib discovery (best-effort) + guarded bridge/resolver imports
# ---------------------------------------------------------------------------
def _locate_aitheros_root() -> Optional[str]:
    """Best-effort: find an AitherOS package root so `lib.compute.*` imports.

    Honors AITHER_OS_ROOT / AITHER_ROOT, else probes a few sibling candidates.
    Returns a path that contains `lib/compute/comfyui_node.py`, or None.
    """
    candidates: List[str] = []
    for env in ("AITHER_OS_ROOT", "AITHEROS_ROOT", "AITHER_ROOT"):
        v = os.environ.get(env)
        if v:
            candidates.append(v)
            candidates.append(os.path.join(v, "AitherOS"))
    here = Path(__file__).resolve()
    # awnode/awnode/comfyui_setup.py -> repo root is parents[2]
    for up in (here.parents[2] if len(here.parents) > 2 else here.parent,):
        candidates.append(str(up / "AitherOS"))
        candidates.append(str(up))
    for c in candidates:
        try:
            if c and os.path.isfile(os.path.join(c, "lib", "compute", "comfyui_node.py")):
                return c
        except Exception:
            continue
    return None


def _ensure_aitheros_on_path() -> Optional[str]:
    root = _locate_aitheros_root()
    if root and root not in sys.path:
        sys.path.insert(0, root)
    return root


def _import_fabric_bridge():
    """Lazy + guarded import of the mesh-join bridge. None if unavailable."""
    _ensure_aitheros_on_path()
    try:
        from lib.compute import comfyui_node  # type: ignore
        return comfyui_node
    except Exception:
        return None


def _import_model_resolver():
    """Lazy + guarded import of the model-profile resolver. None if unavailable."""
    _ensure_aitheros_on_path()
    try:
        from lib.compute import comfyui_models  # type: ignore
        return comfyui_models
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Model profile resolution (fabric resolver, else raw YAML)
# ---------------------------------------------------------------------------
def _yaml_profile_fallback(profile: str) -> Tuple[str, List[str], List[Dict[str, str]]]:
    """Read comfyui-model-profiles.yaml directly when the resolver isn't importable.

    Returns (resolved_profile, model_names, downloads). downloads here is a
    best-effort plan (no presign/RBAC — that needs the fabric); names always work.
    """
    root = _locate_aitheros_root()
    candidates = []
    if root:
        candidates.append(os.path.join(root, "config", "comfyui-model-profiles.yaml"))
    for env in ("AITHER_COMFYUI_MODEL_PROFILES",):
        if os.environ.get(env):
            candidates.append(os.environ[env])
    path = next((p for p in candidates if p and os.path.isfile(p)), None)
    if not path:
        return profile, [], []
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except Exception:
        return profile, [], []
    profiles = doc.get("profiles") or {}
    catalog = doc.get("catalog") or {}
    resolved = profile if profile in profiles else DEFAULT_PROFILE
    names: List[str] = []
    for mid in (profiles.get(resolved, {}) or {}).get("models", []) or []:
        entry = catalog.get(mid, {})
        names.append(entry.get("name", mid))
    return resolved, names, []


def _resolve_models(profile: str, tenant: Optional[str]) -> Dict[str, Any]:
    """Resolve `profile` -> {resolved, names, downloads, env_value, source}."""
    resolver = _import_model_resolver()
    if resolver is not None:
        try:
            resolved = resolver.resolve_profile(profile)
            names = resolver.model_names(resolved)
            downloads = resolver.to_downloads(resolved, tenant=tenant)
            env_value = json.dumps(downloads, separators=(",", ":"))
            return {
                "resolved": resolved,
                "names": names,
                "downloads": downloads,
                "env_value": env_value,
                "source": "fabric-resolver",
            }
        except Exception as e:  # pragma: no cover - fail-safe
            print(f"  ! model resolver error ({e}); falling back to raw YAML")
    resolved, names, downloads = _yaml_profile_fallback(profile)
    return {
        "resolved": resolved,
        "names": names,
        "downloads": downloads,
        "env_value": json.dumps(downloads, separators=(",", ":")),
        "source": "yaml-fallback" if names else "unresolved",
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def _ensure_comfyui_started(
    mgr, port: int, dry_run: bool
) -> Dict[str, Any]:
    """Install + start the comfyui extension, poll health. dry_run -> plan only."""
    manifest = mgr.get_extension(COMFYUI_EXT_ID)
    if not manifest:
        return {"error": f"'{COMFYUI_EXT_ID}' not in extension catalog"}

    # Honor a custom port: the comfyui manifest defaults to 8188.
    if port and port != manifest.port:
        manifest.port = port
        manifest.ports = {COMFYUI_EXT_ID: port}

    if dry_run:
        return {
            "status": "planned",
            "image": manifest.image,
            "port": manifest.port,
            "health_endpoint": manifest.health_endpoint,
        }

    inst = await mgr.install(COMFYUI_EXT_ID)
    if "error" in inst:
        return {"error": inst["error"], "hint": inst.get("hint", "")}

    started = await mgr.start(COMFYUI_EXT_ID)
    if "error" in started:
        return {"error": started["error"], "hint": started.get("hint", "")}

    # Bounded health poll (short — container cold-start may still be pulling weights)
    health = {"status": "unknown"}
    for _ in range(8):
        health = await mgr.health_check(COMFYUI_EXT_ID)
        if health.get("status") == "running":
            break
        await asyncio.sleep(2.0)

    return {
        "status": started.get("status", "running"),
        "url": started.get("url"),
        "port": started.get("port", manifest.port),
        "health": health.get("status", "unknown"),
    }


def _resolve_binding(
    workspace_id: Optional[str], tenant: Optional[str]
) -> Tuple[Optional[str], Optional[str], Any]:
    """Flags win; else fall back to connect/sync state. Returns (ws, tenant, client)."""
    from awnode.sync import get_sync_client

    client = get_sync_client()
    try:
        client._load_local_state()
    except Exception:
        pass
    ws = workspace_id or client.workspace.workspace_id or None
    tn = tenant or client.workspace.tenant_id or None
    # Apply overrides so register/sync POST carries them.
    if ws:
        client.workspace.workspace_id = ws
    if tn:
        client.workspace.tenant_id = tn
    return ws, tn, client


async def _setup_comfyui(
    profile: str = DEFAULT_PROFILE,
    workspace_id: Optional[str] = None,
    tenant: Optional[str] = None,
    mesh_url: Optional[str] = None,
    port: int = 8188,
    skip_models: bool = False,
    dry_run: bool = False,
) -> int:
    """Stand up self-hosted ComfyUI, join the fabric, pull models, bind workspace.

    Returns a process exit code (0 ok, 1 degraded/failed install).
    """
    from awnode.extensions import get_extension_manager

    mesh_url = mesh_url or os.environ.get("AITHERMESH_URL") or os.environ.get("AITHER_MESH_URL")
    if mesh_url:
        # Thread to the fabric bridge's service resolution.
        os.environ.setdefault("AITHERMESH_URL", mesh_url)
        os.environ.setdefault("AITHER_MESH_URL", mesh_url)

    ws_id, tn, sync_client = _resolve_binding(workspace_id, tenant)
    comfy_base = f"http://localhost:{port}"

    print("AwNode :: setup comfyui")
    print("=" * 56)
    print(f"  profile        : {profile}")
    print(f"  port           : {port}  ({comfy_base})")
    print(f"  workspace      : {ws_id or '(from connect state -- none)'}")
    print(f"  tenant         : {tn or '(from connect state -- none)'}")
    print(f"  mesh           : {mesh_url or '(service-resolved / default)'}")
    print(f"  skip-models    : {skip_models}")
    print(f"  DRY-RUN        : {dry_run}")
    print("-" * 56)

    summary: Dict[str, Any] = {"comfy_base": comfy_base}

    # -- (b first) MODELS: resolve so we can inject the env at container start --
    models = {"resolved": profile, "names": [], "downloads": [], "env_value": "[]", "source": "skipped"}
    if not skip_models:
        models = _resolve_models(profile, tn)
    n_models = len(models["names"])
    n_dl = len(models["downloads"])
    if skip_models:
        print(f"[models]  skipped (--skip-models)")
    else:
        print(f"[models]  profile '{models['resolved']}' via {models['source']}: "
              f"{n_models} models, {n_dl} downloads resolved")
        if dry_run:
            for nm in models["names"]:
                print(f"            - {nm}")

    # Inject AITHER_MODEL_DOWNLOADS into the comfyui container env BEFORE start so
    # the entrypoint pulls them on boot (and persist a copy for visibility).
    mgr = get_extension_manager()
    if not skip_models and not dry_run and models["downloads"]:
        manifest = mgr.get_extension(COMFYUI_EXT_ID)
        if manifest is not None:
            manifest.environment = dict(manifest.environment or {})
            manifest.environment["AITHER_MODEL_DOWNLOADS"] = models["env_value"]
        os.environ["AITHER_MODEL_DOWNLOADS"] = models["env_value"]
        try:
            home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
            (home / "comfyui").mkdir(parents=True, exist_ok=True)
            (home / "comfyui" / "model_downloads.json").write_text(
                json.dumps(models["downloads"], indent=2)
            )
        except Exception:
            pass

    # -- (a) INSTALL / START + health --
    start_res = await _ensure_comfyui_started(mgr, port, dry_run)
    summary["install"] = start_res
    if "error" in start_res:
        print(f"[install] FAILED: {start_res['error']}")
        if start_res.get("hint"):
            print(f"          hint: {start_res['hint']}")
        # Install failure is fatal for the rest of the kit.
        _print_summary(summary, models, skip_models, dry_run)
        return 1
    if dry_run:
        print(f"[install] PLAN: pull+run {start_res['image']} on :{start_res['port']}, "
              f"poll {start_res['health_endpoint']} until healthy")
    else:
        print(f"[install] {start_res['status']} at {start_res.get('url')} "
              f"(health: {start_res.get('health')})")

    # -- (c) MESH-JOIN (guarded; degrade gracefully) --
    bridge = _import_fabric_bridge()
    if bridge is None:
        summary["mesh"] = {"status": "bridge-unavailable"}
        print("[mesh]    AitherOS fabric bridge (lib.compute.comfyui_node) not "
              "importable on this box -- skipping mesh join.")
        print("          Workspace bind still applies; node won't be pooled for "
              "fleet routing until the bridge is present.")
    else:
        node_token = (
            os.environ.get("AITHER_NODE_TOKEN")
            or os.environ.get("AITHER_MESH_PSK")
            or os.environ.get("AITHER_API_KEY")
            or None
        )
        try:
            join = bridge.join_fabric(
                comfy_base,
                node_token=node_token,
                role=FABRIC_ROLE,
                tenant=tn,
                workspace_id=ws_id,
                dry_run=dry_run,
            )
        except Exception as e:  # pragma: no cover - join_fabric is itself fail-safe
            join = {"status": "error", "error": str(e)}
        summary["mesh"] = join
        node_id = join.get("node_id", "?")
        if dry_run:
            print(f"[mesh]    PLAN: join as node {node_id} role={FABRIC_ROLE} "
                  f"(psk_present={join.get('psk_present')})")
        else:
            print(f"[mesh]    {join.get('status', '?')}: node {node_id} "
                  f"role={FABRIC_ROLE}")
        if join.get("warning"):
            print(f"          ! {join['warning']}")

    # -- (d) WORKSPACE-BIND --
    if dry_run:
        summary["workspace"] = {"status": "planned", "workspace_id": ws_id, "tenant_id": tn}
        print(f"[bind]    PLAN: register endpoint + sync_workspace -> "
              f"workspace={ws_id or '(server-assigned)'} tenant={tn or '(server-assigned)'}")
    else:
        if not os.environ.get("AITHER_API_KEY"):
            summary["workspace"] = {"status": "no-api-key"}
            print("[bind]    no AITHER_API_KEY -- run `awnode connect <key>` to "
                  "bind this node to your workspace. (ComfyUI is running locally.)")
        else:
            reg = await sync_client.register_endpoint()
            if "error" in reg:
                summary["workspace"] = {"status": "error", "error": reg["error"]}
                print(f"[bind]    register failed: {reg['error']}")
            else:
                await sync_client.sync_workspace()
                bound_ws = sync_client.workspace.workspace_id or ws_id
                bound_tn = sync_client.workspace.tenant_id or tn
                summary["workspace"] = {
                    "status": "bound",
                    "workspace_id": bound_ws,
                    "tenant_id": bound_tn,
                    "node_id": sync_client.endpoint.node_id,
                }
                print(f"[bind]    bound node {sync_client.endpoint.node_id} -> "
                      f"workspace={bound_ws} tenant={bound_tn}")

    _print_summary(summary, models, skip_models, dry_run)
    return 0


def _print_summary(summary, models, skip_models, dry_run) -> None:
    print("-" * 56)
    print("Summary")
    inst = summary.get("install", {})
    inst_line = inst.get("error") and f"FAILED ({inst['error']})" or inst.get("status", "?")
    print(f"  install   : {inst_line}")
    if skip_models:
        print(f"  models    : skipped")
    else:
        verb = "planned" if dry_run else "pulling"
        print(f"  models    : {len(models['names'])} ({verb}) via {models['source']}")
    mesh = summary.get("mesh", {})
    print(f"  mesh      : {mesh.get('status', 'n/a')}"
          + (f" (node {mesh.get('node_id')})" if mesh.get("node_id") else ""))
    ws = summary.get("workspace", {})
    print(f"  workspace : {ws.get('status', 'n/a')}"
          + (f" -> {ws.get('workspace_id')}" if ws.get("workspace_id") else ""))
    print(f"  comfy_base: {summary['comfy_base']}   "
          "(set this as media-forge comfy_base)")
    if dry_run:
        print("  (dry-run -- no install, no model pull, no mesh/workspace POST)")
