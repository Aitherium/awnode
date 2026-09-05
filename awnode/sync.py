"""
Workspace Sync Client
======================

Syncs workspace configuration between local awnode and the AitherOS platform.
Registers this machine as a compute endpoint in the tenant's device fleet.

Sync direction:
  - Cloud -> Local: agent configs, workspace settings, routing preferences
  - Local -> Cloud: hardware specs, available models, health status, inference capacity

Runs as a background task inside awnode server.
"""

import asyncio
import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("awnode.sync")

AITHER_HOME = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
CONFIG_FILE = AITHER_HOME / "config.yaml"
SYNC_STATE_FILE = AITHER_HOME / "sync_state.json"
GATEWAY_URL = os.environ.get("AITHER_CLOUD_URL", "https://portal.aitherium.com")
API_KEY = os.environ.get("AITHER_API_KEY", "")
SYNC_INTERVAL = int(os.environ.get("AITHER_SYNC_INTERVAL", "300"))  # 5 minutes


@dataclass
class EndpointInfo:
    """Hardware and capability info for this endpoint."""

    node_id: str = ""
    hostname: str = ""
    platform: str = ""
    platform_version: str = ""
    python_version: str = ""
    gpu_name: str = ""
    gpu_vram_mb: int = 0
    cpu_count: int = 0
    ram_mb: int = 0
    available_models: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    ollama_available: bool = False
    vllm_available: bool = False
    inference_ready: bool = False
    last_heartbeat: str = ""

    def detect(self):
        """Detect hardware capabilities of this machine."""
        import hashlib

        self.hostname = platform.node() or "unknown"
        self.platform = platform.system()
        self.platform_version = platform.version()
        self.python_version = platform.python_version()
        self.cpu_count = os.cpu_count() or 1

        # Generate stable node_id from hardware fingerprint
        raw = f"{self.hostname}-{platform.machine()}-{self.cpu_count}"
        self.node_id = f"node_{hashlib.sha256(raw.encode()).hexdigest()[:12]}"

        # RAM detection
        try:
            import psutil

            self.ram_mb = int(psutil.virtual_memory().total / 1024 / 1024)
        except ImportError:
            pass

        # GPU detection via nvidia-smi
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                line = result.stdout.strip().split("\n")[0]
                parts = line.rsplit(",", 1)
                self.gpu_name = parts[0].strip()
                self.gpu_vram_mb = int(parts[1].strip()) if len(parts) > 1 else 0
        except Exception:
            pass

        # Capabilities based on detected hardware
        self.capabilities = ["chat", "filesystem", "git", "code_search"]
        if self.gpu_vram_mb > 0:
            self.capabilities.append("gpu_inference")
        if self.gpu_vram_mb >= 16384:
            self.capabilities.append("vllm")

        self.last_heartbeat = datetime.now(timezone.utc).isoformat()

    async def detect_backends(self):
        """Probe for available inference backends (Ollama, vLLM)."""
        # Ollama
        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{ollama_host}/api/tags")
                if r.status_code == 200:
                    self.ollama_available = True
                    data = r.json()
                    self.available_models = [
                        m["name"] for m in data.get("models", [])
                    ]
        except Exception:
            self.ollama_available = False

        # vLLM
        vllm_url = os.environ.get("AITHER_VLLM_URL", "http://localhost:8120")
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{vllm_url}/health")
                self.vllm_available = r.status_code == 200
        except Exception:
            self.vllm_available = False

        self.inference_ready = self.ollama_available or self.vllm_available


@dataclass
class WorkspaceConfig:
    """Workspace configuration synced from cloud."""

    tenant_id: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    agent_routing: Dict[str, str] = field(default_factory=dict)  # agent -> "local"|"cloud"|"auto"
    agent_roster: List[str] = field(default_factory=list)
    inference_contribution: bool = False  # contribute local GPU to pool
    sync_memory: bool = False
    sync_conversations: bool = False
    tier: str = "free"
    settings: Dict[str, Any] = field(default_factory=dict)


class WorkspaceSyncClient:
    """Manages bidirectional sync between local awnode and cloud platform."""

    def __init__(self):
        self.endpoint = EndpointInfo()
        self.workspace = WorkspaceConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._tenant_scoped_token = ""  # Loaded from sync_state.json if available

    def _load_local_state(self):
        """Load saved sync state from disk."""
        if SYNC_STATE_FILE.exists():
            try:
                with open(SYNC_STATE_FILE) as f:
                    data = json.load(f)
                self.workspace.tenant_id = data.get("tenant_id", "")
                self.workspace.workspace_id = data.get("workspace_id", "")
                self.workspace.agent_routing = data.get("agent_routing", {})
                self.workspace.inference_contribution = data.get(
                    "inference_contribution", False
                )
                # Load tenant-scoped token if available (new enrollment flow)
                self._tenant_scoped_token = data.get("tenant_scoped_token", "")
            except Exception:
                pass

    def _get_auth_header(self) -> Dict[str, str]:
        """Get Authorization header using tenant-scoped token or API key."""
        # Prefer tenant-scoped token (new enrollment flow)
        if hasattr(self, "_tenant_scoped_token") and self._tenant_scoped_token:
            return {"Authorization": f"Bearer {self._tenant_scoped_token}"}
        # Fall back to API key (traditional flow)
        if API_KEY:
            return {"Authorization": f"Bearer {API_KEY}"}
        return {}

    def _save_local_state(self):
        """Persist sync state to disk."""
        AITHER_HOME.mkdir(parents=True, exist_ok=True)
        data = {
            "tenant_id": self.workspace.tenant_id,
            "workspace_id": self.workspace.workspace_id,
            "agent_routing": self.workspace.agent_routing,
            "agent_roster": self.workspace.agent_roster,
            "inference_contribution": self.workspace.inference_contribution,
            "tier": self.workspace.tier,
            "last_sync": datetime.now(timezone.utc).isoformat(),
        }
        # Re-persist the enrolled tenant-scoped token so a sync-state rewrite does
        # not wipe it — server.py's remote /chat auth reads it back from this file.
        if self._tenant_scoped_token:
            data["tenant_scoped_token"] = self._tenant_scoped_token
        with open(SYNC_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    async def register_endpoint(self) -> Dict[str, Any]:
        """Register this machine as an endpoint with the cloud platform.

        Returns:
            Registration response dict with tenant_id and workspace config,
            or an error dict if registration fails.
        """
        auth_header = self._get_auth_header()
        if not auth_header:
            return {
                "error": "No credentials configured. Run: "
                "awnode connect --enroll-token=TOKEN or "
                "awnode connect API_KEY"
            }

        self.endpoint.detect()
        await self.endpoint.detect_backends()

        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.post(
                    f"{GATEWAY_URL}/v1/endpoints/register",
                    json=asdict(self.endpoint),
                    headers=auth_header,
                )
                if r.status_code == 200:
                    data = r.json()
                    self.workspace.tenant_id = data.get("tenant_id", "")
                    self.workspace.workspace_id = data.get("workspace_id", "")
                    self._save_local_state()
                    return data
                return {
                    "error": f"Registration failed: HTTP {r.status_code}",
                    "body": r.text[:200],
                }
        except Exception as e:
            return {"error": f"Registration failed: {e}"}

    async def sync_workspace(self) -> Dict[str, Any]:
        """Pull workspace config from cloud, push local status.

        Returns:
            Sync response with workspace config and fleet summary,
            or an error/offline status dict.
        """
        auth_header = self._get_auth_header()
        if not auth_header:
            return {"error": "No credentials configured"}

        self.endpoint.detect()
        await self.endpoint.detect_backends()

        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                # Push heartbeat + pull config in one call
                r = await c.post(
                    f"{GATEWAY_URL}/v1/workspace/sync",
                    json={
                        "endpoint": asdict(self.endpoint),
                        "current_config": {
                            "agent_routing": self.workspace.agent_routing,
                            "inference_contribution": self.workspace.inference_contribution,
                        },
                    },
                    headers=auth_header,
                )
                if r.status_code == 200:
                    data = r.json()
                    # Apply cloud config (cloud is source of truth)
                    if "workspace" in data:
                        ws = data["workspace"]
                        self.workspace.workspace_name = ws.get("name", "")
                        self.workspace.agent_roster = ws.get("agent_roster", [])
                        self.workspace.tier = ws.get("tier", "free")
                        self.workspace.settings = ws.get("settings", {})
                        if "agent_routing" in ws:
                            self.workspace.agent_routing = ws["agent_routing"]
                    self._save_local_state()
                    return data
                return {"error": f"Sync failed: HTTP {r.status_code}"}
        except httpx.ConnectError:
            logger.debug("Cloud unreachable, skipping sync")
            return {
                "status": "offline",
                "message": "Cloud unreachable, using cached config",
            }
        except Exception as e:
            return {"error": f"Sync failed: {e}"}

    async def heartbeat(self) -> Dict[str, Any]:
        """Send a lightweight heartbeat to the cloud.

        Returns:
            Server response dict, or empty dict on failure.
        """
        if not API_KEY:
            return {}

        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(
                    f"{GATEWAY_URL}/v1/endpoints/heartbeat",
                    json={
                        "node_id": self.endpoint.node_id,
                        "inference_ready": self.endpoint.inference_ready,
                        "available_models": self.endpoint.available_models,
                        "gpu_vram_mb": self.endpoint.gpu_vram_mb,
                    },
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
                return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    async def start_background_sync(self):
        """Start periodic background sync loop.

        Performs initial registration, then runs sync_workspace every SYNC_INTERVAL seconds.
        """
        self._running = True
        self._load_local_state()
        self.endpoint.detect()
        await self.endpoint.detect_backends()

        # Initial registration
        await self.register_endpoint()

        self._task = asyncio.create_task(self._sync_loop())
        logger.info("Background sync started (interval: %ds)", SYNC_INTERVAL)

    async def _sync_loop(self):
        """Periodic sync loop. Runs until stop() is called."""
        while self._running:
            try:
                await asyncio.sleep(SYNC_INTERVAL)
                await self.sync_workspace()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Sync error: %s", e)

    async def stop(self):
        """Stop background sync and cancel the loop task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_status(self) -> Dict[str, Any]:
        """Return current sync status for display.

        Returns:
            Dict with workspace config, endpoint info, and sync state.
        """
        return {
            "syncing": self._running,
            "node_id": self.endpoint.node_id,
            "hostname": self.endpoint.hostname,
            "tenant_id": self.workspace.tenant_id,
            "workspace_id": self.workspace.workspace_id,
            "workspace_name": self.workspace.workspace_name,
            "tier": self.workspace.tier,
            "agent_routing": self.workspace.agent_routing,
            "agent_roster": self.workspace.agent_roster,
            "inference_ready": self.endpoint.inference_ready,
            "gpu": self.endpoint.gpu_name or "none",
            "gpu_vram_mb": self.endpoint.gpu_vram_mb,
            "available_models": self.endpoint.available_models,
            "cloud_url": GATEWAY_URL,
            "has_api_key": bool(API_KEY),
        }


# Singleton
_sync_client: Optional[WorkspaceSyncClient] = None


def get_sync_client() -> WorkspaceSyncClient:
    """Get or create the singleton WorkspaceSyncClient."""
    global _sync_client
    if _sync_client is None:
        _sync_client = WorkspaceSyncClient()
    return _sync_client
