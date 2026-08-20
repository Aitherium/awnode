"""
AwNode Extension Manager
==============================

Manages deployable extensions (creative services, agents, tools) that run
alongside awnode on customer hardware.

Extensions are defined by manifests in ~/.aither/extensions/ and can be:
  - Docker containers (pulled and managed automatically)
  - Local processes (started with a command)
  - Remote services (already running, just registered)

Built-in extension catalog:
  - canvas      AitherCanvas + ComfyUI (image generation)
  - comfyui     Standalone ComfyUI (port 8188)
  - ollama      Ollama LLM server
  - iris        Iris creative agent
  - prometheus  Prometheus simulation engine
  - muse        Audio synthesis (MusicGen)
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, asdict, field
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
    DOCKER = "docker"       # Managed Docker container
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
    """Extension definition -- what it is and how to run it."""
    id: str                                # Unique identifier (e.g., "canvas", "comfyui")
    name: str                              # Display name
    description: str                       # What it does
    version: str = "1.0.0"
    type: str = "docker"                   # docker, process, remote
    category: str = "creative"             # creative, agent, inference, tool

    # Docker-specific
    image: str = ""                        # Docker image (e.g., "aitherium/aither-canvas:latest")
    ports: Dict[str, int] = field(default_factory=dict)  # {name: host_port}
    volumes: List[str] = field(default_factory=list)     # Volume mounts
    environment: Dict[str, str] = field(default_factory=dict)
    gpu: bool = False                      # Requires GPU
    gpu_vram_min_mb: int = 0              # Minimum VRAM

    # Process-specific
    command: str = ""                      # Start command
    working_dir: str = ""                  # Working directory

    # Remote-specific
    url: str = ""                          # Base URL of existing service

    # Common
    health_endpoint: str = "/health"       # Health check path
    port: int = 0                          # Primary port
    tools: List[str] = field(default_factory=list)   # MCP tools this extension provides
    agents: List[str] = field(default_factory=list)  # Agents this extension enables
    dependencies: List[str] = field(default_factory=list)  # Other extensions required

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


# -- Built-in Extension Catalog --

BUILTIN_CATALOG: Dict[str, ExtensionManifest] = {
    "canvas": ExtensionManifest(
        id="canvas",
        name="AitherCanvas",
        description="AI image generation powered by ComfyUI. Supports txt2img, img2img, inpainting, outpainting.",
        version="1.0.0",
        type="docker",
        category="creative",
        image="aitherium/aither-canvas:latest",
        ports={"canvas": 8108, "comfyui": 8188},
        gpu=True,
        gpu_vram_min_mb=6144,
        health_endpoint="/health",
        port=8108,
        tools=["generate_image", "refine_image", "inpaint_image", "outpaint_image",
               "generate_batch", "list_workflows", "smart_generate"],
        agents=["iris"],
        environment={"COMFYUI_URL": "http://localhost:8188"},
    ),
    "comfyui": ExtensionManifest(
        id="comfyui",
        name="ComfyUI",
        description="Standalone ComfyUI server for custom workflows. Use with Canvas or directly.",
        version="1.0.0",
        type="docker",
        category="creative",
        image="aitherium/aither-comfyui:latest",
        ports={"comfyui": 8188},
        gpu=True,
        gpu_vram_min_mb=4096,
        health_endpoint="/",
        port=8188,
        tools=["comfyui_queue_prompt", "comfyui_get_history", "comfyui_upload_image"],
    ),
    "comfyui-3d": ExtensionManifest(
        id="comfyui-3d",
        name="ComfyUI 3D",
        description="3D model generation (Hunyuan3D). Converts images to 3D meshes (GLB).",
        version="1.0.0",
        type="docker",
        category="creative",
        image="aitherium/aither-comfyui-3d:latest",
        ports={"comfyui3d": 8289},
        gpu=True,
        gpu_vram_min_mb=8192,
        health_endpoint="/",
        port=8289,
        tools=["generate_character_3d", "image_to_3d"],
    ),
    "muse": ExtensionManifest(
        id="muse",
        name="AitherMuse",
        description="AI audio synthesis -- music generation (MusicGen), sound effects (AudioLDM2), voice.",
        version="1.0.0",
        type="docker",
        category="creative",
        image="aitherium/aither-creative-engine:latest",
        ports={"muse": 8190},
        gpu=True,
        gpu_vram_min_mb=4096,
        health_endpoint="/health",
        port=8190,
        tools=["generate_music", "generate_sfx", "text_to_speech"],
    ),
    "sd-webui": ExtensionManifest(
        id="sd-webui",
        name="Stable Diffusion WebUI",
        description="AUTOMATIC1111 SD WebUI with API mode. Alternative to ComfyUI.",
        version="1.0.0",
        type="docker",
        category="creative",
        image="aitherium/sd-webui:latest",
        ports={"sdwebui": 7860},
        gpu=True,
        gpu_vram_min_mb=4096,
        health_endpoint="/sdapi/v1/progress",
        port=7860,
        tools=["sd_txt2img", "sd_img2img", "sd_extra_single"],
    ),
    "ollama": ExtensionManifest(
        id="ollama",
        name="Ollama",
        description="Local LLM inference server. Pull and run open models.",
        version="1.0.0",
        type="process",
        category="inference",
        command="ollama serve",
        health_endpoint="/api/tags",
        port=11434,
        tools=["ollama_chat", "ollama_generate", "ollama_list_models", "ollama_pull_model"],
    ),
    "iris": ExtensionManifest(
        id="iris",
        name="Iris Agent",
        description="Creative AI agent -- image generation, design, visual content production.",
        version="1.0.0",
        type="remote",
        category="agent",
        url="http://localhost:8108",  # Routes through Canvas
        health_endpoint="/health",
        port=8108,
        agents=["iris"],
        dependencies=["canvas"],
        tools=["generate_image", "design_system_generate", "smart_generate"],
    ),
    "design": ExtensionManifest(
        id="design",
        name="AitherDesign",
        description="Design system tools -- tokens, linting, export to Tailwind/DTCG, storyboards.",
        version="1.0.0",
        type="remote",
        category="tool",
        url="http://localhost:8001",  # Routes through Genesis
        health_endpoint="/health",
        port=8001,
        tools=["design_system_generate", "plan_storyboard", "design_to_code",
               "get_design_tokens", "lint_design_system", "export_design_tokens"],
    ),
    "autorig": ExtensionManifest(
        id="autorig",
        name="AutoRig",
        description="Automatic 3D character rigging via Blender Rigify. GLB in, rigged GLB out.",
        version="1.0.0",
        type="docker",
        category="creative",
        image="aitherium/aither-autorig:latest",
        ports={"autorig": 8794, "rigify": 8792},
        gpu=False,
        health_endpoint="/health",
        port=8794,
        tools=["rig_mesh", "character_pipeline_status"],
        dependencies=["comfyui-3d"],
    ),
}


class ExtensionManager:
    """Manages extension lifecycle: install, start, stop, health check."""

    def __init__(self) -> None:
        EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._states: Dict[str, ExtensionState] = {}
        self._load_states()

    def _load_states(self) -> None:
        """Load persisted extension states."""
        state_file = EXTENSIONS_DIR / "states.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                for ext_id, state_data in data.items():
                    self._states[ext_id] = ExtensionState(**state_data)
            except Exception:
                logger.warning("Failed to load extension states from %s", state_file)

    def _save_states(self) -> None:
        """Persist extension states."""
        state_file = EXTENSIONS_DIR / "states.json"
        data = {ext_id: asdict(state) for ext_id, state in self._states.items()}
        state_file.write_text(json.dumps(data, indent=2))

    def list_catalog(self) -> List[Dict[str, Any]]:
        """List all available extensions (built-in + custom)."""
        result = []
        for ext_id, manifest in BUILTIN_CATALOG.items():
            state = self._states.get(ext_id, ExtensionState(id=ext_id))
            result.append({
                **manifest.to_dict(),
                "status": state.status,
                "url": state.url or f"http://localhost:{manifest.port}",
            })

        # Load custom manifests from extensions dir
        for f in EXTENSIONS_DIR.glob("*.json"):
            if f.name in ("catalog.json", "states.json"):
                continue
            try:
                data = json.loads(f.read_text())
                ext_id = data.get("id", f.stem)
                state = self._states.get(ext_id, ExtensionState(id=ext_id))
                result.append({**data, "status": state.status, "custom": True})
            except Exception:
                logger.warning("Failed to load custom manifest: %s", f)

        return result

    def get_extension(self, ext_id: str) -> Optional[ExtensionManifest]:
        """Get extension manifest by ID."""
        if ext_id in BUILTIN_CATALOG:
            return BUILTIN_CATALOG[ext_id]
        # Check custom
        custom_file = EXTENSIONS_DIR / f"{ext_id}.json"
        if custom_file.exists():
            try:
                data = json.loads(custom_file.read_text())
                return ExtensionManifest(**data)
            except Exception:
                logger.warning("Failed to parse custom manifest: %s", custom_file)
        return None

    async def install(self, ext_id: str) -> Dict[str, Any]:
        """Install an extension (pull Docker image or verify process)."""
        # License check — is this extension allowed by the subscription?
        try:
            from awnode.license import get_license_manager
            denial = get_license_manager().check_extension(ext_id)
            if denial:
                return {"error": denial, "scope": "subscription"}
        except ImportError:
            pass

        manifest = self.get_extension(ext_id)
        if not manifest:
            return {"error": f"Unknown extension: {ext_id}"}

        state = self._states.get(ext_id, ExtensionState(id=ext_id))

        if manifest.type == "docker":
            if not manifest.image:
                return {"error": f"No Docker image specified for {ext_id}"}

            # Check GPU requirement
            if manifest.gpu:
                try:
                    result = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.total",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0:
                        vram = int(result.stdout.strip().split("\n")[0])
                        if vram < manifest.gpu_vram_min_mb:
                            return {
                                "error": (
                                    f"Insufficient GPU VRAM. "
                                    f"Need {manifest.gpu_vram_min_mb}MB, have {vram}MB"
                                ),
                                "hint": "Use --cloud flag to run this in Elysium instead",
                            }
                    else:
                        return {
                            "error": "No GPU detected. This extension requires NVIDIA GPU.",
                            "hint": "Use --cloud flag to run this in Elysium instead",
                        }
                except FileNotFoundError:
                    return {
                        "error": "nvidia-smi not found. This extension requires NVIDIA GPU.",
                        "hint": "Use --cloud flag to run this in Elysium instead",
                    }

            # Pull image
            logger.info("Pulling Docker image: %s", manifest.image)
            try:
                result = subprocess.run(
                    ["docker", "pull", manifest.image],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode != 0:
                    return {"error": f"Docker pull failed: {result.stderr[:200]}"}
            except FileNotFoundError:
                return {
                    "error": "Docker not installed. Install Docker Desktop first.",
                    "url": "https://docker.com/products/docker-desktop",
                }

        elif manifest.type == "process":
            # Verify the command exists
            cmd = manifest.command.split()[0]
            import shutil
            if not shutil.which(cmd):
                return {
                    "error": f"'{cmd}' not found. Install it first.",
                    "hint": f"See {manifest.description}",
                }

        state.status = ExtensionStatus.INSTALLED
        state.installed_at = datetime.now(timezone.utc).isoformat()
        self._states[ext_id] = state
        self._save_states()

        return {"status": "installed", "extension": ext_id, "type": manifest.type}

    async def start(self, ext_id: str) -> Dict[str, Any]:
        """Start an installed extension."""
        manifest = self.get_extension(ext_id)
        if not manifest:
            return {"error": f"Unknown extension: {ext_id}"}

        state = self._states.get(ext_id, ExtensionState(id=ext_id))

        # Check dependencies
        for dep in manifest.dependencies:
            dep_state = self._states.get(dep)
            if not dep_state or dep_state.status != ExtensionStatus.RUNNING:
                return {
                    "error": f"Dependency '{dep}' is not running. Start it first.",
                    "hint": f"awnode ext start {dep}",
                }

        if manifest.type == "docker":
            container_name = f"awnode-{ext_id}"

            # Build docker run command
            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--restart", "unless-stopped",
            ]

            # Port mappings
            for _name, port in manifest.ports.items():
                cmd.extend(["-p", f"127.0.0.1:{port}:{port}"])

            # GPU
            if manifest.gpu:
                cmd.extend(["--gpus", "all"])

            # Environment
            for key, val in manifest.environment.items():
                cmd.extend(["-e", f"{key}={val}"])

            # Volumes
            for vol in manifest.volumes:
                cmd.extend(["-v", vol])

            cmd.append(manifest.image)

            try:
                # Stop existing container if any
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True, timeout=10,
                )

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    state.status = ExtensionStatus.ERROR
                    state.error = result.stderr[:200]
                    self._states[ext_id] = state
                    self._save_states()
                    return {"error": f"Failed to start: {result.stderr[:200]}"}

                state.container_id = result.stdout.strip()[:12]
            except Exception as e:
                return {"error": f"Docker start failed: {e}"}

        elif manifest.type == "process":
            try:
                proc = subprocess.Popen(
                    manifest.command.split(),
                    cwd=manifest.working_dir or None,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                state.pid = proc.pid
            except Exception as e:
                return {"error": f"Process start failed: {e}"}

        state.status = ExtensionStatus.RUNNING
        state.started_at = datetime.now(timezone.utc).isoformat()
        state.port = manifest.port
        state.url = manifest.url or f"http://localhost:{manifest.port}"
        self._states[ext_id] = state
        self._save_states()

        return {
            "status": "running",
            "extension": ext_id,
            "url": state.url,
            "port": state.port,
            "tools": manifest.tools,
            "agents": manifest.agents,
        }

    async def stop(self, ext_id: str) -> Dict[str, Any]:
        """Stop a running extension."""
        state = self._states.get(ext_id)
        if not state:
            return {"error": f"Extension '{ext_id}' not found"}

        manifest = self.get_extension(ext_id)

        if manifest and manifest.type == "docker":
            container_name = f"awnode-{ext_id}"
            subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True, timeout=30,
            )
            subprocess.run(
                ["docker", "rm", container_name],
                capture_output=True, timeout=10,
            )

        elif state.pid:
            try:
                os.kill(state.pid, 15)  # SIGTERM
            except ProcessLookupError:
                pass

        state.status = ExtensionStatus.STOPPED
        state.container_id = ""
        state.pid = 0
        self._states[ext_id] = state
        self._save_states()

        return {"status": "stopped", "extension": ext_id}

    async def health_check(self, ext_id: str) -> Dict[str, Any]:
        """Check if an extension is healthy."""
        manifest = self.get_extension(ext_id)
        state = self._states.get(ext_id, ExtensionState(id=ext_id))

        if not manifest:
            return {"status": "unknown", "error": "Not in catalog"}

        url = state.url or f"http://localhost:{manifest.port}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{url.rstrip('/')}{manifest.health_endpoint}")
                healthy = r.status_code == 200
                state.last_health = datetime.now(timezone.utc).isoformat()
                if healthy:
                    state.status = ExtensionStatus.RUNNING
                else:
                    state.status = ExtensionStatus.ERROR
                    state.error = f"Health check returned HTTP {r.status_code}"
                self._states[ext_id] = state
                self._save_states()
                return {"status": state.status, "url": url, "http_status": r.status_code}
        except Exception as e:
            if state.status == ExtensionStatus.RUNNING:
                state.status = ExtensionStatus.ERROR
            state.error = str(e)
            self._states[ext_id] = state
            self._save_states()
            return {"status": "unreachable", "url": url, "error": str(e)}

    async def health_check_all(self) -> Dict[str, Any]:
        """Health check all installed extensions."""
        results = {}
        for ext_id, state in self._states.items():
            if state.status in (ExtensionStatus.RUNNING, ExtensionStatus.ERROR):
                results[ext_id] = await self.health_check(ext_id)
        return results

    def get_active_tools(self) -> List[str]:
        """Return MCP tool names provided by running extensions."""
        tools = []
        for ext_id, state in self._states.items():
            if state.status == ExtensionStatus.RUNNING:
                manifest = self.get_extension(ext_id)
                if manifest:
                    tools.extend(manifest.tools)
        return tools

    def get_active_agents(self) -> List[str]:
        """Return agent names enabled by running extensions."""
        agents = []
        for ext_id, state in self._states.items():
            if state.status == ExtensionStatus.RUNNING:
                manifest = self.get_extension(ext_id)
                if manifest:
                    agents.extend(manifest.agents)
        return agents

    def register_custom(self, manifest_data: Dict[str, Any]) -> Dict[str, Any]:
        """Register a custom extension from a manifest dict."""
        ext_id = manifest_data.get("id", "")
        if not ext_id:
            return {"error": "Extension manifest must have an 'id' field"}

        manifest_file = EXTENSIONS_DIR / f"{ext_id}.json"
        manifest_file.write_text(json.dumps(manifest_data, indent=2))
        return {"status": "registered", "extension": ext_id, "path": str(manifest_file)}


# Singleton
_manager: Optional[ExtensionManager] = None


def get_extension_manager() -> ExtensionManager:
    """Get the singleton ExtensionManager instance."""
    global _manager
    if _manager is None:
        _manager = ExtensionManager()
    return _manager
