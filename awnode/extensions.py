"""
awnode Extension Manager
========================

Manages deployable extensions (creative services, agents, tools) that run
alongside awnode on customer hardware.

**Since 2026-09-06 the catalog is the awdk component loader, not a list here.**
`awdk.adk.addon_manager` reads component manifests from three sources (a pip-installed
brick's `aither.components` entry point, `~/.aither/components/*.yaml`, the bundled
`adk/addon_manifests/`) and this manager renders them in the shape `awnode ext` has
always shown. The nine entries that used to be hardcoded in BUILTIN_CATALOG (canvas,
comfyui, comfyui-3d, muse, sd-webui, ollama, iris, design, autorig) are manifests now,
so `awnode ext start canvas` runs the same declaration awdk, the aitheros launcher, awsh
and the Living Desktop read. Two hosts with two catalogs of "what can run here" was the
silence this ended: an extension present in one and absent from the other looked like
an extension nobody wanted.

Legacy custom manifests in ~/.aither/extensions/*.json still load, and are the only
thing the old docker-CLI path here still starts. Everything else delegates to awdk.

BUILTIN_CATALOG is kept as an EMPTY dict on purpose -- ACM008 in
check_aw_component_manifests.py asserts it stays empty, so a rival catalog cannot grow
back here one entry at a time.
"""

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("awnode.extensions")

AITHER_HOME = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
EXTENSIONS_DIR = AITHER_HOME / "extensions"
CATALOG_FILE = EXTENSIONS_DIR / "catalog.json"


class ExtensionType(str, Enum):
    DOCKER = "docker"       # Managed container
    PROCESS = "process"     # Local process (started with command)
    REMOTE = "remote"       # Pre-existing service (just health check)


class ExtensionStatus(str, Enum):
    AVAILABLE = "available"     # In catalog, not installed
    INSTALLED = "installed"     # Downloaded/pulled, not running
    RUNNING = "running"         # Active and healthy
    STOPPED = "stopped"         # Was running, now stopped
    ERROR = "error"             # Failed to start or unhealthy
    UPGRADING = "upgrading"     # Being updated


@dataclass
class ExtensionManifest:
    """Extension definition -- what it is and how to run it (the awnode-facing shape)."""
    id: str
    name: str
    description: str
    version: str = "1.0.0"
    type: str = "docker"                   # docker, process, remote
    category: str = "creative"             # creative, agent, inference, tool

    image: str = ""
    ports: Dict[str, int] = field(default_factory=dict)
    volumes: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    gpu: bool = False
    gpu_vram_min_mb: int = 0

    command: str = ""
    working_dir: str = ""

    url: str = ""

    health_endpoint: str = "/health"
    port: int = 0
    tools: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)

    #: Set when the manifest came from the awdk component loader; lifecycle is delegated.
    brick: str = ""
    source: str = ""
    surfaces: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExtensionState:
    """Runtime state of an installed extension."""
    id: str
    status: str = "available"
    container_id: str = ""
    pid: int = 0
    port: int = 0
    url: str = ""
    installed_at: str = ""
    started_at: str = ""
    last_health: str = ""
    error: str = ""


#: EMPTY on purpose (see the module docstring). Do not add entries here: add a
#: manifest under the addon manifest directory instead. ACM008 asserts this.
BUILTIN_CATALOG: Dict[str, ExtensionManifest] = {}

_ADDON_TYPE = {"docker": "docker", "process": "process", "external": "remote",
               "capability": "remote", "quadlet": "docker", "podman": "docker"}


def _from_addon(m: Dict[str, Any]) -> ExtensionManifest:
    """The awdk addon manifest -> the awnode extension shape, field by field."""
    port = int(m.get("default_port") or 0)
    res = m.get("resources") or {}
    ports = dict(m.get("ports") or {})
    if port and not ports:
        ports = {m["id"]: port}
    return ExtensionManifest(
        id=str(m["id"]),
        name=str(m.get("name") or m["id"]),
        description=str(m.get("description") or ""),
        version=str(m.get("version") or "1.0.0"),
        type=_ADDON_TYPE.get(str(m.get("type") or "docker"), "remote"),
        category=str(m.get("category") or ("agent" if m.get("agents") else "tool")),
        image=str(m.get("image") or ""),
        ports=ports,
        volumes=[v if isinstance(v, str) else f"{v.get('name')}:{v.get('path')}"
                 for v in (m.get("volumes") or [])],
        environment={str(k): str(v) for k, v in (m.get("env_defaults") or {}).items()},
        gpu=bool(res.get("gpu", False)),
        gpu_vram_min_mb=int(res.get("vram_gb") or 0) * 1024,
        command=str(m.get("command") or ""),
        url=str(m.get("endpoint") or ""),
        health_endpoint=str((m.get("health_check") or {}).get("path") or "/health"),
        port=port,
        tools=list(m.get("tools") or []),
        agents=list(m.get("agents") or []),
        dependencies=list(m.get("dependencies") or []),
        brick=str(m.get("brick") or ""),
        source=str(m.get("_source") or "awdk"),
        surfaces=dict(m.get("surfaces") or {}),
    )


def _awdk_manifests() -> Dict[str, Dict[str, Any]]:
    """Every component manifest the awdk loader knows, keyed by id. Empty if awdk is absent."""
    try:
        from adk.addon_manager import load_all_manifests
    except ImportError as e:  # awdk is a declared dependency; say so rather than hide it
        logger.warning("awdk component loader unavailable (%s) -- only custom manifests load", e)
        return {}
    try:
        return {str(m["id"]): m for m in load_all_manifests()}
    except Exception as e:  # noqa: BLE001 -- one bad source must not empty the catalog
        logger.warning("awdk component loader failed: %s", e)
        return {}


class ExtensionManager:
    """Manages extension lifecycle: install, start, stop, health check.

    awdk-sourced components delegate to ``adk.addon_manager.AddonManager`` (one
    lifecycle engine); legacy custom JSON manifests keep the docker-CLI path below.
    """

    def __init__(self) -> None:
        EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._states: Dict[str, ExtensionState] = {}
        self._load_states()

    def _load_states(self) -> None:
        state_file = EXTENSIONS_DIR / "states.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                for ext_id, state_data in data.items():
                    self._states[ext_id] = ExtensionState(**state_data)
            except (OSError, ValueError, TypeError) as e:
                logger.warning("Failed to load extension states from %s: %s", state_file, e)

    def _save_states(self) -> None:
        state_file = EXTENSIONS_DIR / "states.json"
        data = {ext_id: asdict(state) for ext_id, state in self._states.items()}
        state_file.write_text(json.dumps(data, indent=2))

    # -- catalog ---------------------------------------------------------------

    def _custom_manifests(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for f in EXTENSIONS_DIR.glob("*.json"):
            if f.name in ("catalog.json", "states.json"):
                continue
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError) as e:
                logger.warning("Failed to load custom manifest %s: %s", f, e)
                continue
            ext_id = data.get("id", f.stem)
            data["id"] = ext_id
            out[ext_id] = data
        return out

    def list_catalog(self) -> List[Dict[str, Any]]:
        """All available extensions: awdk components + legacy custom manifests."""
        result = []
        seen = set()
        for ext_id, m in sorted(_awdk_manifests().items()):
            manifest = _from_addon(m)
            state = self._states.get(ext_id, ExtensionState(id=ext_id))
            fallback = f"http://127.0.0.1:{manifest.port}" if manifest.port else ""
            url = state.url or manifest.url or fallback
            result.append({**manifest.to_dict(), "status": state.status, "url": url})
            seen.add(ext_id)
        for ext_id, data in sorted(self._custom_manifests().items()):
            if ext_id in seen:
                continue
            state = self._states.get(ext_id, ExtensionState(id=ext_id))
            result.append({**data, "status": state.status, "custom": True})
        return result

    def get_extension(self, ext_id: str) -> Optional[ExtensionManifest]:
        m = _awdk_manifests().get(ext_id)
        if m:
            return _from_addon(m)
        data = self._custom_manifests().get(ext_id)
        if data:
            try:
                known = {f for f in ExtensionManifest.__dataclass_fields__}
                return ExtensionManifest(**{k: v for k, v in data.items() if k in known})
            except TypeError as e:
                logger.warning("Failed to parse custom manifest %s: %s", ext_id, e)
        return None

    def _is_awdk(self, ext_id: str) -> bool:
        return ext_id in _awdk_manifests()

    # -- lifecycle ---------------------------------------------------------------

    def _record(self, ext_id: str, inst: Any, manifest: ExtensionManifest) -> ExtensionState:
        """Mirror an awdk AddonInstance into this manager's state shape."""
        state = self._states.get(ext_id, ExtensionState(id=ext_id))
        status = str(getattr(inst, "status", "") or "")
        state.status = {"running": ExtensionStatus.RUNNING, "starting": ExtensionStatus.RUNNING,
                        "stopped": ExtensionStatus.STOPPED, "error": ExtensionStatus.ERROR,
                        }.get(status, ExtensionStatus.INSTALLED)
        state.url = str(getattr(inst, "endpoint", "") or "")
        state.port = manifest.port
        state.pid = int(getattr(inst, "pid", 0) or 0)
        state.container_id = str(getattr(inst, "container_id", "") or "")
        state.error = str(getattr(inst, "error_message", "") or "")
        state.started_at = datetime.now(timezone.utc).isoformat()
        self._states[ext_id] = state
        self._save_states()
        return state

    async def install(self, ext_id: str) -> Dict[str, Any]:
        """Install an extension (pull image or verify command)."""
        try:
            from awnode.license import get_license_manager
            denial = get_license_manager().check_extension(ext_id)
            if denial:
                return {"error": denial, "scope": "subscription"}
        except ImportError:
            logger.debug("license manager absent; no subscription gate applied")

        manifest = self.get_extension(ext_id)
        if not manifest:
            return {"error": f"Unknown extension: {ext_id}"}
        state = self._states.get(ext_id, ExtensionState(id=ext_id))

        if manifest.type == "docker" and not self._is_awdk(ext_id):
            if not manifest.image:
                return {"error": f"No Docker image specified for {ext_id}"}
            if manifest.gpu:
                denial = _gpu_denial(manifest)
                if denial:
                    return denial
            logger.info("Pulling image: %s", manifest.image)
            try:
                result = subprocess.run(  # blocking-ok: legacy CLI path, no live loop
                    ["docker", "pull", manifest.image],
                    capture_output=True, text=True, timeout=600,
                                        encoding="utf-8", errors="replace")
                if result.returncode != 0:
                    return {"error": f"Docker pull failed: {result.stderr[:200]}"}
            except FileNotFoundError:
                return {"error": "Docker not installed. Install Docker Desktop first.",
                        "url": "https://docker.com/products/docker-desktop"}
        elif manifest.type == "process" and manifest.command:
            import shutil
            cmd = manifest.command.split()[0]
            if not shutil.which(cmd):
                return {"error": f"'{cmd}' not found. Install it first.",
                        "hint": f"See {manifest.description}"}

        state.status = ExtensionStatus.INSTALLED
        state.installed_at = datetime.now(timezone.utc).isoformat()
        self._states[ext_id] = state
        self._save_states()
        return {"status": "installed", "extension": ext_id, "type": manifest.type,
                "source": manifest.source or "custom"}

    async def start(self, ext_id: str) -> Dict[str, Any]:
        """Start an installed extension."""
        manifest = self.get_extension(ext_id)
        if not manifest:
            return {"error": f"Unknown extension: {ext_id}"}

        for dep in manifest.dependencies:
            dep_state = self._states.get(dep)
            if not dep_state or dep_state.status != ExtensionStatus.RUNNING:
                return {"error": f"Dependency '{dep}' is not running. Start it first.",
                        "hint": f"awnode ext start {dep}"}

        if self._is_awdk(ext_id):
            from adk.addon_manager import AddonManager
            try:
                inst = await AddonManager().enable(ext_id)
            except (ValueError, RuntimeError, OSError) as e:
                return {"error": f"awdk could not enable {ext_id}: {e}"}
            state = self._record(ext_id, inst, manifest)
            if state.status == ExtensionStatus.ERROR:
                return {"error": state.error or f"{ext_id} failed to start"}
            return {"status": "running", "extension": ext_id, "url": state.url,
                    "port": state.port, "tools": manifest.tools, "agents": manifest.agents,
                    "source": manifest.source}

        state = self._states.get(ext_id, ExtensionState(id=ext_id))
        if manifest.type == "docker":
            container_name = f"awnode-{ext_id}"
            cmd = ["docker", "run", "-d", "--name", container_name, "--restart", "unless-stopped"]
            for _name, port in manifest.ports.items():
                cmd.extend(["-p", f"127.0.0.1:{port}:{port}"])
            if manifest.gpu:
                cmd.extend(["--gpus", "all"])
            for key, val in manifest.environment.items():
                cmd.extend(["-e", f"{key}={val}"])
            for vol in manifest.volumes:
                cmd.extend(["-v", vol])
            cmd.append(manifest.image)
            try:
                subprocess.run(  # blocking-ok: legacy CLI path, no live loop
                    ["docker", "rm", "-f", container_name], capture_output=True, timeout=10)
                result = subprocess.run(  # blocking-ok: legacy CLI path, no live loop
                    cmd, capture_output=True, text=True, timeout=30,
                                        encoding="utf-8", errors="replace")
                if result.returncode != 0:
                    state.status = ExtensionStatus.ERROR
                    state.error = result.stderr[:200]
                    self._states[ext_id] = state
                    self._save_states()
                    return {"error": f"Failed to start: {result.stderr[:200]}"}
                state.container_id = result.stdout.strip()[:12]
            except (OSError, subprocess.SubprocessError) as e:
                return {"error": f"Docker start failed: {e}"}
        elif manifest.type == "process":
            try:
                proc = subprocess.Popen(manifest.command.split(), cwd=manifest.working_dir or None,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                state.pid = proc.pid
            except (OSError, ValueError) as e:
                return {"error": f"Process start failed: {e}"}

        state.status = ExtensionStatus.RUNNING
        state.started_at = datetime.now(timezone.utc).isoformat()
        state.port = manifest.port
        state.url = manifest.url or f"http://127.0.0.1:{manifest.port}"
        self._states[ext_id] = state
        self._save_states()
        return {"status": "running", "extension": ext_id, "url": state.url, "port": state.port,
                "tools": manifest.tools, "agents": manifest.agents, "source": "custom"}

    async def stop(self, ext_id: str) -> Dict[str, Any]:
        """Stop a running extension."""
        state = self._states.get(ext_id)
        if not state:
            return {"error": f"Extension '{ext_id}' not found"}
        if self._is_awdk(ext_id):
            from adk.addon_manager import AddonManager
            await AddonManager().disable(ext_id)
        else:
            manifest = self.get_extension(ext_id)
            if manifest and manifest.type == "docker":
                container_name = f"awnode-{ext_id}"
                subprocess.run(  # blocking-ok: legacy CLI path, no live loop
                    ["docker", "stop", container_name], capture_output=True, timeout=30)
                subprocess.run(  # blocking-ok: legacy CLI path, no live loop
                    ["docker", "rm", container_name], capture_output=True, timeout=10)
            elif state.pid:
                try:
                    os.kill(state.pid, 15)
                except ProcessLookupError:
                    logger.debug("extension %s pid %s already gone", ext_id, state.pid)
        state.status = ExtensionStatus.STOPPED
        state.container_id = ""
        state.pid = 0
        self._states[ext_id] = state
        self._save_states()
        return {"status": "stopped", "extension": ext_id}

    async def health_check(self, ext_id: str) -> Dict[str, Any]:
        """Check if an extension is healthy -- by ASKING it, never by its process state."""
        manifest = self.get_extension(ext_id)
        state = self._states.get(ext_id, ExtensionState(id=ext_id))
        if not manifest:
            return {"status": "unknown", "error": "Not in catalog"}
        url = state.url or manifest.url or f"http://127.0.0.1:{manifest.port}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{url.rstrip('/')}{manifest.health_endpoint}")
                healthy = r.status_code == 200
                state.last_health = datetime.now(timezone.utc).isoformat()
                state.status = ExtensionStatus.RUNNING if healthy else ExtensionStatus.ERROR
                if not healthy:
                    state.error = f"Health check returned HTTP {r.status_code}"
                self._states[ext_id] = state
                self._save_states()
                return {"status": state.status, "url": url, "http_status": r.status_code}
        except (httpx.HTTPError, OSError) as e:
            if state.status == ExtensionStatus.RUNNING:
                state.status = ExtensionStatus.ERROR
            state.error = str(e)
            self._states[ext_id] = state
            self._save_states()
            return {"status": "unreachable", "url": url, "error": str(e)}

    async def health_check_all(self) -> Dict[str, Any]:
        results = {}
        for ext_id, state in list(self._states.items()):
            if state.status in (ExtensionStatus.RUNNING, ExtensionStatus.ERROR):
                results[ext_id] = await self.health_check(ext_id)
        return results

    def get_active_tools(self) -> List[str]:
        tools: List[str] = []
        for ext_id, state in self._states.items():
            if state.status == ExtensionStatus.RUNNING:
                manifest = self.get_extension(ext_id)
                if manifest:
                    tools.extend(manifest.tools)
        return tools

    def get_active_agents(self) -> List[str]:
        agents: List[str] = []
        for ext_id, state in self._states.items():
            if state.status == ExtensionStatus.RUNNING:
                manifest = self.get_extension(ext_id)
                if manifest:
                    agents.extend(manifest.agents)
        return agents

    def register_custom(self, manifest_data: Dict[str, Any]) -> Dict[str, Any]:
        """Register a custom extension from a manifest dict (legacy JSON shape)."""
        ext_id = manifest_data.get("id", "")
        if not ext_id:
            return {"error": "Extension manifest must have an 'id' field"}
        manifest_file = EXTENSIONS_DIR / f"{ext_id}.json"
        manifest_file.write_text(json.dumps(manifest_data, indent=2))
        return {"status": "registered", "extension": ext_id, "path": str(manifest_file)}


def _gpu_denial(manifest: ExtensionManifest) -> Optional[Dict[str, Any]]:
    """The old GPU gate for legacy docker manifests, unchanged in behaviour."""
    try:
        result = subprocess.run(  # blocking-ok: legacy CLI path, no live loop
                                ["nvidia-smi", "--query-gpu=memory.total",
                                 "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"error": "nvidia-smi not found. This extension requires NVIDIA GPU.",
                "hint": "Use --cloud flag to run this in Elysium instead"}
    if result.returncode != 0:
        return {"error": "No GPU detected. This extension requires NVIDIA GPU.",
                "hint": "Use --cloud flag to run this in Elysium instead"}
    vram = int(result.stdout.strip().split("\n")[0])
    if vram < manifest.gpu_vram_min_mb:
        return {"error": f"Insufficient GPU VRAM. Need {manifest.gpu_vram_min_mb}MB, have {vram}MB",
                "hint": "Use --cloud flag to run this in Elysium instead"}
    return None


# Singleton
_manager: Optional[ExtensionManager] = None


def get_extension_manager() -> ExtensionManager:
    """Get the singleton ExtensionManager instance."""
    global _manager
    if _manager is None:
        _manager = ExtensionManager()
    return _manager
