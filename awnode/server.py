"""
awnode Standalone Server
=============================

Lightweight FastAPI server that provides:
1. Chat proxy (Genesis or Ollama fallback)
2. MCP tools (filesystem, git, code search — local only)
3. Health/status endpoint
4. Cloud registration (optional)

This is the STANDALONE version — runs without Docker, without Genesis.
When Genesis is available, it proxies. When not, it falls back to local.
"""

import json
import os
import re
import logging
import uuid
from typing import Any, Dict, Optional
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from awnode.sync import get_sync_client, API_KEY as SYNC_API_KEY
from awnode.license import get_license_manager
from awnode.proxy import router as proxy_router

logger = logging.getLogger("awnode")

# ── Configuration ───────────────────────────────────────────────────

GENESIS_URL = os.environ.get("AITHER_URL", "http://localhost:8001")
VLLM_URL = os.environ.get("AITHER_VLLM_URL", os.environ.get("VLLM_URL", "http://localhost:8120"))
LLAMACPP_URL = os.environ.get("LLAMACPP_URL", "http://localhost:8200")
# Bonsai-27B llama.cpp server (PrismML fork). Host-published port of the
# in-fleet Bonsai serving container is 8092; standalone installs can point this
# anywhere an OpenAI-compatible llama.cpp server listens.
BONSAI_URL = os.environ.get("AITHER_BONSAI_URL", "http://localhost:8092")
BONSAI_MODEL = os.environ.get("AITHER_BONSAI_MODEL", "bonsai-27b")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
ELYSIUM_URL = os.environ.get("AITHER_CLOUD_URL", "https://mcp.aitherium.com")
NODE_PORT = int(os.environ.get("AITHERNODE_PORT", "8090"))
NODE_BIND_HOST = os.environ.get("AITHERNODE_BIND_HOST", "127.0.0.1")
API_KEY = os.environ.get("AITHER_API_KEY", "")
MODE = os.environ.get("AITHERNODE_MODE", "auto")  # auto, local, cloud, standalone
VLLM_MODEL = os.environ.get("VLLM_SERVED_MODEL", os.environ.get("AITHER_DEFAULT_MODEL", ""))

# Trusted proxies for /chat remote-auth: only consult X-Forwarded-For when the
# DIRECT peer is one of these IPs (default empty = never trust XFF). Prevents a
# non-browser client from spoofing a loopback IP to bypass the Bearer gate.
TRUSTED_PROXIES = {
    ip.strip()
    for ip in os.environ.get("AITHERNODE_TRUSTED_PROXIES", "").split(",")
    if ip.strip()
}

# ── Filesystem root for read-only MCP tools ──────────────────────────────
# Default to a dedicated shared folder that does NOT contain sensitive files.
# Users can override with AITHERNODE_FS_ROOT environment variable.
def _get_default_fs_root() -> Path:
    """Determine the default filesystem root for MCP tools.

    Default: ~/AitherShared (created if absent)
    Never defaults to home, /, or dirs with typical secrets.
    """
    aither_shared = Path.home() / "AitherShared"
    if not aither_shared.exists():
        try:
            aither_shared.mkdir(parents=True, exist_ok=True)
            # Create a README explaining the purpose of this folder
            readme = aither_shared / "README.txt"
            if not readme.exists():
                readme.write_text(
                    "AitherShared\n"
                    "============\n\n"
                    "Files in this directory are readable by agents connected from aitherium.com.\n"
                    "This is a dedicated shared folder intended for non-sensitive data.\n"
                    "Do NOT place secrets (.ssh, .aws, credentials, etc.) here.\n"
                )
        except Exception as e:
            logger.warning(f"Failed to create default AitherShared directory: {e}")
    return aither_shared

AITHERNODE_FS_ROOT = Path(os.environ.get("AITHERNODE_FS_ROOT", str(_get_default_fs_root())))


# ── Backend detection ───────────────────────────────────────────────

async def _probe(url: str, path: str = "/health") -> bool:
    """Quick health probe."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{url.rstrip('/')}{path}")
            return r.status_code == 200
    except Exception:
        return False


async def _probe_openai(url: str) -> bool:
    """Probe an inference backend via /v1/models — NOT /health.

    A bare /health check misclassifies arbitrary co-resident services as
    LLM backends (live-found: media-forge on :8200 answered /health and got
    picked as 'llama.cpp', then 404'd every chat). Only a 200 on /v1/models
    proves an OpenAI-compatible server.
    """
    return await _probe(url, "/v1/models")


class BackendState:
    """Tracks which backends are available."""
    genesis: bool = False
    vllm: bool = False
    llamacpp: bool = False
    bonsai: bool = False
    ollama: bool = False
    cloud: bool = False
    mode: str = "standalone"

    async def refresh(self):
        self.genesis = await _probe(GENESIS_URL)
        self.vllm = await _probe_openai(VLLM_URL)
        self.llamacpp = await _probe_openai(LLAMACPP_URL)
        self.bonsai = await _probe_openai(BONSAI_URL)
        # Ollama has no /health; /api/tags is its liveness surface
        self.ollama = await _probe(OLLAMA_URL, "/api/tags")
        if API_KEY:
            self.cloud = await _probe(ELYSIUM_URL)

        if MODE == "cloud":
            self.mode = "cloud" if self.cloud else "standalone"
        elif MODE == "local":
            if self.genesis:
                self.mode = "genesis"
            elif self.vllm:
                self.mode = "vllm"
            elif self.llamacpp:
                self.mode = "llamacpp"
            elif self.bonsai:
                self.mode = "bonsai"
            elif self.ollama:
                self.mode = "ollama"
            else:
                self.mode = "standalone"
        else:  # auto — prefer genesis > vllm > llamacpp > bonsai > cloud > ollama
            if self.genesis:
                self.mode = "genesis"
            elif self.vllm:
                self.mode = "vllm"
            elif self.llamacpp:
                self.mode = "llamacpp"
            elif self.bonsai:
                self.mode = "bonsai"
            elif self.cloud and API_KEY:
                self.mode = "cloud"
            elif self.ollama:
                self.mode = "ollama"
            else:
                self.mode = "standalone"


_state = BackendState()

# ── Memory state ────────────────────────────────────────────────────
# Internal conversation-history store (node-only, NOT exposed to the browser —
# there is no /memory/* endpoint). Persists to SQLite under ~/.aither/memory/.
_memory = None


def _get_memory():
    """Lazy-init the node's conversation memory store (adk). Returns None if
    adk memory is unavailable, so /chat degrades to stateless with a warning."""
    global _memory
    if _memory is None:
        try:
            from adk.memory import Memory
            _memory = Memory(agent_name="awnode")
            logger.info("awnode memory initialized at %s", _memory._db_path)
        except Exception as e:
            logger.warning(
                "Memory initialization failed (non-fatal): %s. Chat will be stateless.", e
            )
            _memory = False  # Mark as tried-and-failed so we don't retry every call
    return _memory if _memory is not False else None


# ── Lifespan ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _state.refresh()
    logger.info(f"awnode started -- mode: {_state.mode}")
    logger.info(f"  Genesis:  {'UP' if _state.genesis else 'DOWN'}")
    logger.info(f"  vLLM:     {'UP' if _state.vllm else 'DOWN'} ({VLLM_URL})")
    logger.info(f"  llama.cpp:{'UP' if _state.llamacpp else 'DOWN'} ({LLAMACPP_URL})")
    logger.info(f"  Bonsai:   {'UP' if _state.bonsai else 'DOWN'} ({BONSAI_URL})")
    logger.info(f"  Ollama:   {'UP' if _state.ollama else 'DOWN'}")
    logger.info(f"  Cloud:    {'UP' if _state.cloud else 'DOWN'}")
    logger.info(f"  MCP FS Root: {AITHERNODE_FS_ROOT.resolve()} (override with AITHERNODE_FS_ROOT)")

    # Warn if bound to all interfaces without proper authentication
    if NODE_BIND_HOST in ("0.0.0.0", "::", "[::]:"):
        logger.warning(
            "WARNING: awnode is binding to all interfaces (%s). "
            "File access is protected by bearer token for non-loopback clients, but prefer "
            "AITHERNODE_BIND_HOST=127.0.0.1 for local-only access unless a tunnel/gateway is in place.",
            NODE_BIND_HOST
        )

    # Validate subscription license
    lic = get_license_manager()
    try:
        lic_result = await lic.validate()
        logger.info("  License: %s (plan=%s)", lic_result.get("status"), lic_result.get("plan"))
    except Exception as e:
        logger.warning("  License: FAILED (%s)", e)

    # Start workspace sync if API key is configured
    sync = get_sync_client()
    if SYNC_API_KEY:
        try:
            await sync.start_background_sync()
            logger.info("  Sync:    ACTIVE (tenant: %s)", sync.workspace.tenant_id or "pending")
        except Exception as e:
            logger.warning("  Sync:    FAILED (%s)", e)
    else:
        logger.info("  Sync:    DISABLED (no API key)")

    yield

    # Stop background sync
    await sync.stop()
    logger.info("awnode stopped")


# ── App ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="awnode",
    description="Lightweight local gateway for AitherOS",
    version="0.1.0",
    lifespan=lifespan,
)

# Lock CORS to the aitherium.com family + localhost. A wildcard ("*") lets ANY
# website the user visits call this loopback node cross-origin — and since every
# browser request arrives from 127.0.0.1, the loopback file-access rule can't tell
# aitherium.com from a drive-by site. The regex makes the browser fail the preflight
# for foreign origins before the request ever executes. Mirrors _check_origin().
ALLOWED_ORIGIN_REGEX = (
    r"^https?://([a-z0-9-]+\.)*aitherium\.com$"
    r"|^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# Private Network Access (PNA) support for Chrome.
# When an HTTPS page (e.g., https://portal.aitherium.com) probes a private/loopback
# HTTP endpoint (e.g., http://127.0.0.1:8090/health), Chrome requires a preflight
# OPTIONS response with 'Access-Control-Allow-Private-Network: true' to allow the
# health check. This middleware adds that header ONLY for /health route,
# and ONLY when the Origin is in the CORS allowlist.
#
# Scope: Narrowly applied only to health route + allowlisted origins.
# This prevents any cross-site from requesting PNA access via this server.
@app.middleware("http")
async def add_private_network_access(request, call_next):
    origin = request.headers.get("origin", "")
    # Only add PNA header for health route when Origin is in allowlist
    if request.url.path == "/health" and origin and re.match(ALLOWED_ORIGIN_REGEX, origin):
        response = await call_next(request)
        # Add PNA header to allow Chrome to proceed with cross-origin private requests
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response
    return await call_next(request)


# ── Models ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    stream: bool = False


class NodeStatus(BaseModel):
    status: str = "ok"
    mode: str = ""
    genesis: bool = False
    vllm: bool = False
    llamacpp: bool = False
    bonsai: bool = False
    ollama: bool = False
    cloud: bool = False
    version: str = "0.2.0"
    port: int = NODE_PORT


# ── Endpoints ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "awnode", "mode": _state.mode}


@app.get("/status")
async def status():
    await _state.refresh()
    return NodeStatus(
        mode=_state.mode,
        genesis=_state.genesis,
        vllm=_state.vllm,
        llamacpp=_state.llamacpp,
        bonsai=_state.bonsai,
        ollama=_state.ollama,
        cloud=_state.cloud,
    )


# ── /chat remote-auth helpers ────────────────────────────────────────────
# Local callers (loopback) chat anonymously for backward compat; REMOTE callers
# (reaching the node through the authenticated tunnel/gateway) must present the
# enrolled account Bearer token. Client IP is derived from the DIRECT peer and
# only overridden by X-Forwarded-For when the direct peer is a configured
# trusted proxy — the same non-spoofable rule the file-read surface uses.


def _get_client_ip(request: Request, x_forwarded_for: Optional[str]) -> str:
    """Direct-peer IP, overridden by X-Forwarded-For only from a trusted proxy."""
    direct_ip = request.client.host if request.client else "unknown"
    if x_forwarded_for and direct_ip in TRUSTED_PROXIES:
        # XFF may carry multiple hops; the first is the origin client.
        xff_ip = x_forwarded_for.split(",")[0].strip()
        logger.debug("Trusting X-Forwarded-For %s from trusted proxy %s", xff_ip, direct_ip)
        return xff_ip
    return direct_ip


def _is_local_client(client_ip: str) -> bool:
    """Loopback peer (plus 'testclient' for FastAPI TestClient)."""
    return client_ip in ("127.0.0.1", "localhost", "::1", "testclient")


def _get_enrolled_token_created_at() -> Optional[float]:
    """Read the enrolled token's creation epoch from sync_state.json (for expiry)."""
    try:
        aither_home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
        sync_state_file = aither_home / "sync_state.json"
        if not sync_state_file.exists():
            return None
        data = json.loads(sync_state_file.read_text(encoding="utf-8"))
        return data.get("tenant_scoped_token_created_at")
    except Exception as e:
        logger.debug("Failed to read token created_at: %s", e)
        return None


def _is_token_expired(created_at: Optional[float]) -> bool:
    """True if the enrolled token is older than 90 days. Missing timestamp is
    treated as not-expired (fail-open on age only — the token must still match)."""
    import time
    if created_at is None:
        return False
    return (time.time() - created_at) > (90 * 24 * 60 * 60)


@app.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_forwarded_for: Optional[str] = Header(None),
):
    """Stateful chat proxy with remote-auth gating and internal conversation memory.

    Auth:
      - Local (loopback) callers: anonymous (backward compat).
      - Remote callers: require a valid, non-expired Authorization: Bearer <enrolled_token>
        (compared in constant time; X-Forwarded-For trusted only from a configured proxy).
    Memory:
      - History is recalled before inference and the turn persisted after, keyed by
        conversation_id (generated if absent). Internal to the node — no browser surface.
      - Degrades to stateless if adk memory is unavailable.
    """
    import httpx
    import hmac

    # ── Auth gate (outside the try below so 401/403 are never masked as 500) ──
    client_ip = _get_client_ip(request, x_forwarded_for)
    if not _is_local_client(client_ip):
        if not authorization or not authorization.startswith("Bearer "):
            logger.warning("Unauthorized chat attempt from %s (no bearer token)", client_ip)
            raise HTTPException(status_code=401, detail="Bearer token required for remote access")
        provided_token = authorization[7:].strip()
        enrolled_token = _get_enrolled_bearer_token()
        if not enrolled_token or not hmac.compare_digest(provided_token, enrolled_token):
            logger.warning("Invalid bearer token from %s", client_ip)
            raise HTTPException(status_code=403, detail="Invalid or expired bearer token")
        if _is_token_expired(_get_enrolled_token_created_at()):
            logger.warning("Expired bearer token from %s (>90 days)", client_ip)
            raise HTTPException(
                status_code=403,
                detail="Bearer token expired (>90 days); re-enroll with 'adk enroll'",
            )
        logger.info("Remote chat request from %s (authenticated)", client_ip)
    else:
        logger.debug("Local chat request from %s", client_ip)

    # ── Memory recall ──
    conversation_id = req.conversation_id or str(uuid.uuid4())
    history = []
    memory = _get_memory()
    if memory:
        try:
            history = await memory.get_history(conversation_id, limit=20)
            if history:
                logger.debug("Recalled %d messages for conversation %s", len(history), conversation_id)
        except Exception as e:
            logger.warning("Memory recall failed (stateless fallback): %s", e)
            history = []

    # Full message list for OpenAI-style backends: prior turns + the new user turn.
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": req.message})

    response_text = ""
    model_used = ""
    tokens_info = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    via = "unknown"

    try:
        if _state.mode == "genesis":
            async with httpx.AsyncClient(timeout=120.0) as c:
                headers = {}
                if API_KEY:
                    headers["Authorization"] = f"Bearer {API_KEY}"
                payload = {
                    "message": req.message,
                    "conversation_id": conversation_id,
                    "model": req.model,
                    "stream": False,
                }
                r = await c.post(f"{GENESIS_URL}/chat", json=payload, headers=headers)
                data = r.json()
                response_text = data.get("response", data.get("content", ""))
                model_used = data.get("model_used", "genesis")
                via = "genesis-proxy"

        elif _state.mode == "vllm":
            # Direct vLLM — OpenAI-compatible API (no Genesis pipeline)
            async with httpx.AsyncClient(timeout=120.0) as c:
                model = req.model or VLLM_MODEL or "default"
                payload = {
                    "model": model, "messages": messages, "max_tokens": 4096, "stream": False,
                    # Customer GPU nodes serve bonsai-27b-awq here (cuda-vllm-*.yaml), and it
                    # carries the same Qwen3 template that force-OPENS `<think>`. vLLM has no
                    # server-side `--reasoning-budget` backstop like llama.cpp, so the caller
                    # is the ONLY place this can be bounded. Templates without the variable
                    # ignore it, so this is safe for non-reasoning models too.
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                r = await c.post(f"{VLLM_URL}/v1/chat/completions", json=payload)
                data = r.json()
                if data.get("choices"):
                    response_text = data["choices"][0].get("message", {}).get("content", "")
                usage = data.get("usage", {})
                tokens_info = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                model_used = data.get("model", model)
                via = "vllm-direct"

        elif _state.mode == "llamacpp":
            # llama.cpp server exposes OpenAI-compatible /v1/chat/completions
            async with httpx.AsyncClient(timeout=120.0) as c:
                model = req.model or "default"
                payload = {
                    "model": model, "messages": messages, "max_tokens": 4096, "stream": False,
                    # Bonsai's Qwen3 template force-OPENS `<think>` when enable_thinking is
                    # undefined; with no cap a 1-bit 27B burns the whole budget mid-thought,
                    # drifts language, and leaves `content` empty. Ask for a direct answer.
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                r = await c.post(f"{LLAMACPP_URL}/v1/chat/completions", json=payload)
                data = r.json()
                if data.get("choices"):
                    response_text = data["choices"][0].get("message", {}).get("content", "")
                usage = data.get("usage", {})
                tokens_info = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                model_used = data.get("model", model)
                via = "llamacpp-direct"

        elif _state.mode == "bonsai":
            # Bonsai-27B via the PrismML llama.cpp fork — OpenAI-compatible /v1.
            # Reasoning model: give it token budget for <think> or content is empty.
            async with httpx.AsyncClient(timeout=120.0) as c:
                model = req.model or BONSAI_MODEL
                payload = {
                    "model": model, "messages": messages, "max_tokens": 4096, "stream": False,
                    # See llamacpp mode above — unbounded forced `<think>` is what made this
                    # answer in raw reasoning (and drift to CJK) instead of replying.
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                r = await c.post(f"{BONSAI_URL}/v1/chat/completions", json=payload)
                data = r.json()
                if data.get("choices"):
                    response_text = data["choices"][0].get("message", {}).get("content", "")
                usage = data.get("usage", {})
                tokens_info = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                model_used = data.get("model", model)
                via = "bonsai-direct"

        elif _state.mode == "cloud":
            async with httpx.AsyncClient(timeout=120.0) as c:
                headers = {"Authorization": f"Bearer {API_KEY}"}
                payload = {
                    "message": req.message,
                    "conversation_id": conversation_id,
                    "model": req.model,
                    "stream": False,
                }
                r = await c.post(f"{ELYSIUM_URL}/api/chat", json=payload, headers=headers)
                data = r.json()
                response_text = data.get("response", data.get("content", ""))
                model_used = data.get("model_used", "cloud")
                via = "cloud-proxy"

        elif _state.mode == "ollama":
            async with httpx.AsyncClient(timeout=120.0) as c:
                payload = {"model": req.model or "llama3.2:3b", "messages": messages, "stream": False}
                r = await c.post(f"{OLLAMA_URL}/api/chat", json=payload)
                data = r.json()
                response_text = data.get("message", {}).get("content", "")
                model_used = data.get("model", "ollama")
                via = "ollama-direct"

        else:
            raise HTTPException(503, "No LLM backend available. Install Ollama or connect to Elysium.")

        # Persist the turn (user + assistant) after a successful generation.
        if memory and response_text:
            try:
                await memory.add_message(conversation_id, "user", req.message)
                await memory.add_message(conversation_id, "assistant", response_text)
                logger.debug("Persisted turn to memory for conversation %s", conversation_id)
            except Exception as e:
                logger.warning("Memory persist failed (non-fatal): %s", e)

        return {
            "response": response_text,
            "conversation_id": conversation_id,
            "model_used": model_used,
            "tokens": tokens_info,
            "metadata": {"elapsed_ms": 0, "effort_level": 3, "via": via},
        }

    except HTTPException:
        # Deliberate status codes (e.g. 503 no-backend) must propagate unchanged.
        raise
    except Exception as e:
        logger.error("Chat endpoint error: %s", e)
        raise HTTPException(500, f"Chat processing failed: {str(e)}")


@app.post("/deploy")
async def deploy(target: str = "docker", service: str = "", config: dict = None):
    """Deploy via AitherComet (if Genesis available)."""
    if not _state.genesis:
        raise HTTPException(503, "Deployment requires Genesis. Start AitherOS or connect to cloud.")

    import httpx
    async with httpx.AsyncClient(timeout=60.0) as c:
        headers = {}
        if API_KEY:
            headers["Authorization"] = f"Bearer {API_KEY}"
        payload = {
            "service": service,
            "target": target,
            "config": config or {},
        }
        # Route through MeshCore/Comet
        r = await c.post(f"{GENESIS_URL}/deploy/deploy", json=payload, headers=headers)
        return r.json()


@app.get("/deploy/status")
async def deploy_status():
    """Get deployment status from Comet."""
    if not _state.genesis:
        return {"status": "disconnected", "message": "Genesis not available"}

    import httpx
    async with httpx.AsyncClient(timeout=10.0) as c:
        headers = {}
        if API_KEY:
            headers["Authorization"] = f"Bearer {API_KEY}"
        try:
            r = await c.get(f"{GENESIS_URL}/deploy/deployments", headers=headers)
            return r.json()
        except Exception:
            return {"status": "error", "deployments": []}


@app.post("/connect")
async def connect_to_cloud(api_key: str = "", email: str = ""):
    """Register this node with Elysium cloud."""
    from adk.client import GatewayClient

    gw = GatewayClient(api_key=api_key or API_KEY)

    if email and not api_key:
        # Need to register first
        return {"error": "Use `aither connect` or provide api_key"}

    try:
        result = await gw.register_agent(
            name=f"node-{os.environ.get('COMPUTERNAME', 'local')}",
            capabilities=["chat", "code", "filesystem", "git"],
            description="awnode local gateway",
        )
        return {"status": "connected", "agent_id": result.get("agent_id", "")}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ── Workspace Sync Endpoints ──────────────────────────────────────


class AgentRoutingConfig(BaseModel):
    routing: Dict[str, str] = {}  # agent_name -> "local"|"cloud"|"auto"


class WorkspaceConfigUpdate(BaseModel):
    agent_routing: Optional[Dict[str, str]] = None
    inference_contribution: Optional[bool] = None


@app.get("/sync/status")
async def sync_status():
    """Get current workspace sync status."""
    sync = get_sync_client()
    return sync.get_status()


@app.post("/sync/register")
async def sync_register():
    """Force re-registration with the cloud platform."""
    sync = get_sync_client()
    result = await sync.register_endpoint()
    return result


@app.post("/sync/now")
async def sync_now():
    """Trigger an immediate workspace sync."""
    sync = get_sync_client()
    result = await sync.sync_workspace()
    return result


@app.get("/config")
async def get_config():
    """Get current workspace configuration including agent routing."""
    sync = get_sync_client()
    return {
        "workspace": {
            "tenant_id": sync.workspace.tenant_id,
            "workspace_id": sync.workspace.workspace_id,
            "workspace_name": sync.workspace.workspace_name,
            "tier": sync.workspace.tier,
            "agent_roster": sync.workspace.agent_roster,
            "agent_routing": sync.workspace.agent_routing,
            "inference_contribution": sync.workspace.inference_contribution,
            "settings": sync.workspace.settings,
        },
        "endpoint": {
            "node_id": sync.endpoint.node_id,
            "hostname": sync.endpoint.hostname,
            "gpu": sync.endpoint.gpu_name or "none",
            "gpu_vram_mb": sync.endpoint.gpu_vram_mb,
            "inference_ready": sync.endpoint.inference_ready,
            "available_models": sync.endpoint.available_models,
        },
    }


@app.put("/config")
async def update_config(update: WorkspaceConfigUpdate):
    """Update local workspace configuration and push to cloud.

    Changes are applied locally and synced to the cloud on next sync cycle.
    Use POST /sync/now to push immediately.
    """
    sync = get_sync_client()

    if update.agent_routing is not None:
        valid_modes = {"local", "cloud", "auto"}
        for agent, mode in update.agent_routing.items():
            if mode not in valid_modes:
                raise HTTPException(
                    400,
                    f"Invalid routing mode '{mode}' for agent '{agent}'. "
                    f"Must be one of: {', '.join(sorted(valid_modes))}",
                )
        sync.workspace.agent_routing = update.agent_routing

    if update.inference_contribution is not None:
        sync.workspace.inference_contribution = update.inference_contribution

    sync._save_local_state()
    return {"status": "updated", "config": sync.workspace.__dict__}


# ── MCP tool listing (browser/agent discovery) ─────────────────────
#
# The persistent `awnode start` server exposes the SAME tool registry the
# `awnode mcp` server serves to Claude Code / Cursor / awdk, so the
# AitherConnect extension, an IDE, and the adk all read ONE always-on source of
# The persistent `aithernode start` server exposes the SAME tool registry the
# `aithernode mcp` server serves to Claude Code / Cursor / aither-adk, so the
# Awconnect extension, an IDE, and the adk all read ONE always-on source of
# truth for "what can this node do" instead of each probing a different surface.
# This is listing/discovery only — tool EXECUTION stays on the authenticated MCP
# server (`awnode mcp`), which enforces the loopback/bearer gate.

_tool_registry_cache: Optional[list] = None


def _list_node_tools() -> list:
    """Discover the node's tools via the shared MCP registry. Cached."""
    global _tool_registry_cache
    if _tool_registry_cache is not None:
        return _tool_registry_cache
    tools: list = []
    try:
        from awnode.mcp_server import (
            _discover_tools,
            _mode_allows_scope,
            RuntimeMode,
        )
        registry = _discover_tools()
        # Map the node's backend mode to the MCP scope model: a reachable
        # Genesis (or any local backend) = LOCAL (platform tools allowed),
        # cloud = CLOUD, else STANDALONE (shell-local only).
        if _state.genesis or _state.vllm or _state.llamacpp or _state.bonsai or _state.ollama:
            mcp_mode = RuntimeMode.LOCAL
        elif _state.cloud:
            mcp_mode = RuntimeMode.CLOUD
        else:
            mcp_mode = RuntimeMode.STANDALONE
        for name, info in registry.items():
            if not _mode_allows_scope(mcp_mode, info["scope"]):
                continue
            tools.append({
                "name": name,
                "description": info.get("description", ""),
                "inputSchema": info.get("schema", {}),
                "scope": info.get("scope", ""),
            })
    except Exception as e:
        logger.warning("Tool discovery failed: %s", e)
        return []
    _tool_registry_cache = sorted(tools, key=lambda t: t["name"])
    return _tool_registry_cache


@app.get("/mcp/tools")
async def mcp_tools():
    """List the tools this node exposes (discovery for the extension / IDE)."""
    await _state.refresh()
    tools = _list_node_tools()
    return {"tools": tools, "count": len(tools), "mode": _state.mode}


@app.post("/mcp")
async def mcp_jsonrpc(request: Request):
    """Minimal JSON-RPC surface for tools/list (what Awconnect node tier
    calls). tools/call is intentionally NOT served here — execution lives on the
    authenticated `awnode mcp` server; this endpoint returns a clear error
    directing callers there rather than silently accepting a call it won't run."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON-RPC body")
    method = body.get("method")
    rpc_id = body.get("id")
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"tools": _list_node_tools()},
        }
    if method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32601,
                "message": "tool execution is served by 'awnode mcp' "
                           "(stdio/SSE), not the gateway server",
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


# ── Pack install (marketplace `adk`-method → in-browser) ───────────
#
# Materializes a BUNDLED adk agent pack into ~/.aither/agents/<pack> and marks
# it required, so the AitherConnect "Install" button actually installs instead
# of just showing a command. Mirrors the proven fail-closed logic of awdk
# it required, so the Awconnect "Install" button actually installs instead
# of just showing a command. Mirrors the proven fail-closed logic of aither-adk
# admin_api /admin/packs/apply:
#   - only packs BUNDLED with the node's adk build are installable (the safety
#     moat — never an arbitrary path/upload); unknown pack -> 404 + available list
#   - the extension falls back to the copyable `adk install pack/<id>` command
#     for registry/community packs this endpoint doesn't carry.
# Gate: loopback callers pass; a non-loopback caller needs the enrolled bearer
# (same contract as /chat) — a browser install must not be drivable from the LAN.


def _adk_packs_dir() -> Optional[Path]:
    try:
        import importlib.util
        spec = importlib.util.find_spec("adk")
        if not spec or not spec.origin:
            return None
        d = Path(spec.origin).parent / "packs"
        return d if d.is_dir() else None
    except Exception:
        return None


def _bundled_packs() -> list:
    d = _adk_packs_dir()
    if not d:
        return []
    return sorted(p.name for p in d.glob("*") if (p / "agent.yaml").exists())


def _installed_packs() -> list:
    agents_dir = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "agents"
    if not agents_dir.is_dir():
        return []
    return sorted(p.name for p in agents_dir.glob("*") if (p / "agent.yaml").exists())


def _install_gate(request: Request, authorization: Optional[str]) -> None:
    """Loopback passes; remote caller needs the enrolled bearer. Fail-closed."""
    import hmac
    client_ip = _get_client_ip(request, request.headers.get("x-forwarded-for"))
    if _is_local_client(client_ip):
        return
    token = _get_enrolled_bearer_token()
    provided = authorization[7:].strip() if (authorization or "").startswith("Bearer ") else ""
    if token and provided and hmac.compare_digest(provided, token):
        return
    raise HTTPException(403, "pack install requires a loopback caller or the enrolled bearer token")


@app.get("/packs/installed")
async def packs_installed():
    """List bundled (installable) and installed agent packs — powers the
    marketplace's data-driven 'Installed' state instead of per-pack probes."""
    return {"bundled": _bundled_packs(), "installed": _installed_packs()}


def _toolpacks_dir() -> Path:
    """Where sovereign-installed tool packs land — the dir ToolPackLoader scans."""
    return Path(os.environ.get("AITHER_PACKS_DIR", str(Path.home() / ".aitheros" / "packs")))


def _safe_extract_toolpack(blob: bytes, dest_root: Path, pack: str) -> Path:
    """Extract a downloaded tool-pack .tar.gz into ``dest_root/<pack>`` SAFELY.

    Fail-closed against tar path-traversal (CVE-2007-4559 class): every member
    must be a regular file or dir, carry no absolute path / ``..`` segment / link,
    and resolve to stay inside the destination. Rejects the whole archive on any
    bad member rather than skipping it. Returns the installed pack dir."""
    import io
    import tarfile
    import shutil

    dest = (dest_root / pack).resolve()
    dest_root = dest_root.resolve()
    if dest_root not in dest.parents and dest != dest_root:
        raise HTTPException(400, "invalid pack destination")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise HTTPException(502, "empty pack archive")
        saw_manifest = False
        for m in members:
            if m.islnk() or m.issym() or m.isdev():
                raise HTTPException(400, f"unsafe archive member (link/dev): {m.name}")
            if not (m.isfile() or m.isdir()):
                raise HTTPException(400, f"unsafe archive member type: {m.name}")
            name = m.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise HTTPException(400, f"unsafe archive path: {m.name}")
            target = (dest_root / name).resolve()
            if dest_root not in target.parents and target != dest_root:
                raise HTTPException(400, f"archive path escapes destination: {m.name}")
            if name.split("/")[-1] == ".toolpack.yaml":
                saw_manifest = True
        if not saw_manifest:
            raise HTTPException(502, "archive is not a tool pack (no .toolpack.yaml)")
        # Members validated — extract into a temp sibling, then atomically swap.
        staging = dest.parent / f".{pack}.incoming"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            # filter="data" is a SECOND fail-closed layer (Py3.12+): it rejects
            # absolute paths, traversal, and links independently of the manual
            # member validation above.
            try:
                tar.extractall(staging, filter="data")
            except TypeError:  # pragma: no cover — very old Python without filter=
                tar.extractall(staging)  # noqa: S202 — members individually validated above
            extracted = staging / pack
            if not extracted.is_dir():
                raise HTTPException(502, "archive did not contain the expected pack dir")
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(extracted), str(dest))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return dest


async def _install_registry_toolpack(pack: str, authorization: Optional[str]):
    """Try to install a NON-bundled pack from the marketplace's sovereign
    download endpoint. Returns a response dict on success, raises HTTPException
    for a definitive premium-refusal, or returns None when the pack isn't a
    downloadable registry pack (caller then falls through to the adk-hint 404)."""
    import httpx
    from .proxy import _verify

    # On-box the marketplace is plain HTTP on the fleet network; a sovereign
    # off-box node sets AITHER_MARKETPLACE_URL to the public https edge.
    base = os.environ.get(
        "AITHER_MARKETPLACE_URL", "http://aitheros-marketplace:8260"
    ).rstrip("/")
    url = f"{base}/v1/marketplace/packs/{pack}/download"
    headers = {}
    if authorization:
        headers["Authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=_verify(), follow_redirects=True) as c:
            resp = await c.get(url, headers=headers)
    except httpx.HTTPError:
        return None  # marketplace unreachable → not installable this way
    if resp.status_code == 404:
        return None  # unknown to the marketplace → fall through to adk hint
    if resp.status_code == 403:
        # Premium/entitlement-gated: source is not distributed — managed only.
        raise HTTPException(403, {
            "error": "premium_pack_managed_only",
            "pack": pack,
            "hint": "This pack is paid/entitlement-gated — install it on your "
                    "hosted agent via apply-pack, not on a sovereign node.",
        })
    if resp.status_code != 200:
        raise HTTPException(502, f"marketplace download failed: HTTP {resp.status_code}")
    dest = _safe_extract_toolpack(resp.content, _toolpacks_dir(), pack)
    return {
        "ok": True,
        "pack": pack,
        "kind": "tool_pack",
        "installed_to": str(dest),
        "reloaded": False,
        "note": "tool pack installed — restart the adk agent (or reload packs) to load its tools",
    }


@app.post("/packs/install")
async def packs_install(request: Request, authorization: Optional[str] = Header(default=None)):
    _install_gate(request, authorization)
    import shutil

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    # Accept "pack", "id", or a marketplace "pack/<id>" form.
    pack = str((body or {}).get("pack") or (body or {}).get("id") or "").strip()
    if pack.startswith("pack/"):
        pack = pack[len("pack/"):]
    # Fail-closed name validation — no traversal, no separators (admin_api parity).
    if not pack or "/" in pack or "\\" in pack or pack.startswith("."):
        raise HTTPException(400, "valid pack name required")

    packs_dir = _adk_packs_dir()
    bundled = (packs_dir / pack) if packs_dir else None
    if not bundled or not (bundled / "agent.yaml").exists():
        # Not a bundled adk agent pack. Try the marketplace registry path
        # (free tool packs install sovereign; premium refuse → managed).
        registry = await _install_registry_toolpack(pack, authorization)
        if registry is not None:
            return registry
        # Neither bundled nor a downloadable registry pack.
        raise HTTPException(
            404,
            f"pack '{pack}' is not bundled with this node's adk build "
            f"(installable: {', '.join(_bundled_packs())}) and is not a free "
            f"registry tool pack; run `adk install pack/{pack}` if it is a "
            f"community pack, or apply it to your hosted agent if it is paid",
        )

    dest = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "agents" / pack
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(bundled, dest)
    except OSError as e:
        raise HTTPException(500, f"pack copy failed: {e}") from e

    # Best-effort: hot-apply to a running local adk agent so tools appear without
    # a restart (mirrors admin_api's reload). If no agent is running, the pack is
    # installed on disk and loads on next agent start — reported in the response.
    reloaded = False
    try:
        import httpx
        adk_admin = os.environ.get("AITHER_ADK_ADMIN_URL", "http://127.0.0.1:8790")
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.post(f"{adk_admin}/admin/packs/apply", json={"pack": pack})
            reloaded = r.status_code == 200
    except Exception:
        reloaded = False

    return {
        "ok": True,
        "pack": pack,
        "installed_to": str(dest),
        "reloaded": reloaded,
        "note": None if reloaded else "installed on disk — restart the adk agent to load its tools",
    }


# ── License & Subscription ─────────────────────────────────────


@app.get("/license")
async def license_status():
    """Get current subscription/license status and entitlements."""
    lic = get_license_manager()
    return lic.to_dict()


@app.post("/license/validate")
async def license_validate():
    """Force re-validation of subscription against ACTA."""
    lic = get_license_manager()
    result = await lic.validate(force=True)
    return {**result, "entitlement": lic.to_dict()}


# ── Portal UI Proxy ────────────────────────────────────────────


PORTAL_URL = os.environ.get("AITHER_PORTAL_URL", "https://app.aitherium.com")


@app.get("/")
async def portal_redirect():
    """Redirect browser to portal with auto-auth."""
    from fastapi.responses import HTMLResponse

    api_key = API_KEY or SYNC_API_KEY
    sync = get_sync_client()  # used by the HTML below (sync.endpoint.hostname)

    # Serve a landing page that redirects to portal with auth context
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>awnode</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #0a0a0f; color: #e0e0e0; display: flex; align-items: center;
               justify-content: center; height: 100vh; margin: 0; }}
        .card {{ background: #13131a; border: 1px solid #2a2a3a; border-radius: 12px;
                 padding: 40px; max-width: 480px; text-align: center; }}
        h1 {{ color: #7c6bff; margin-bottom: 8px; }}
        .status {{ font-size: 14px; color: #888; margin-bottom: 24px; }}
        a.btn {{ display: inline-block; background: #7c6bff; color: white; padding: 12px 32px;
                 border-radius: 8px; text-decoration: none; font-weight: 600; margin: 8px; }}
        a.btn:hover {{ background: #6b5ce7; }}
        a.secondary {{ background: #2a2a3a; }}
        .info {{ font-size: 13px; color: #666; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>awnode</h1>
        <div class="status">
            Mode: {_state.mode} &bull;
            Plan: {get_license_manager().entitlement.resolved_plan} &bull;
            Node: {sync.endpoint.hostname or 'local'}
        </div>
        <a class="btn" href="{PORTAL_URL}?node=localhost:{NODE_PORT}&key={api_key[:8] + '...' if api_key else 'none'}"
           target="_blank">Open Portal Dashboard</a>
        <br>
        <a class="btn secondary" href="/status">API Status</a>
        <a class="btn secondary" href="/license">License</a>
        <a class="btn secondary" href="/config">Config</a>
        <div class="info">
            MCP endpoint: <code>http://localhost:{NODE_PORT}/sse</code><br>
            API docs: <a href="/docs" style="color:#7c6bff">/docs</a>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


# ── OpenAI-compatible inference surface ────────────────────────────
#
# Lets any OpenAI-style client (the Awconnect extension's provider tier,
# curl, SDKs) talk to the node directly. Model routing:
#   - model starting with "bonsai"  -> Bonsai llama.cpp server
#   - anything else                 -> best available OpenAI-compatible
#     backend (vllm > llamacpp > bonsai > ollama)
# Streaming responses are relayed chunk-for-chunk (SSE passthrough).


def _openai_backend_for(model: str) -> Optional[str]:
    m = (model or "").lower()
    if m.startswith("bonsai"):
        return BONSAI_URL if _state.bonsai else None
    if _state.vllm:
        return VLLM_URL
    if _state.llamacpp:
        return LLAMACPP_URL
    if _state.bonsai:
        return BONSAI_URL
    if _state.ollama:
        return OLLAMA_URL
    return None


_UNPINNED_MODELS = {"", "auto", "default"}


async def _resolve_backend_model(base: str, model: str) -> str:
    """Rewrite unpinned model names ('auto') to what the backend actually serves.

    The Awconnect extension sends model="auto" in node tier; vLLM and
    llama.cpp 404 on unknown model ids, so resolve to the first served model.
    """
    import httpx

    if model.lower() not in _UNPINNED_MODELS:
        return model
    if base == BONSAI_URL:
        return BONSAI_MODEL
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{base}/v1/models")
            data = r.json().get("data") or []
            if data:
                return data[0].get("id") or model
    except (httpx.HTTPError, ValueError):
        pass
    return model


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    import httpx

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")

    model = str(payload.get("model") or "")
    base = _openai_backend_for(model)
    if not base:
        await _state.refresh()
        base = _openai_backend_for(model)
    if not base:
        raise HTTPException(
            503,
            "no OpenAI-compatible backend available "
            "(start the Bonsai llama.cpp server, vLLM, or Ollama)",
        )
    if base == BONSAI_URL and not model.lower().startswith("bonsai"):
        # llama.cpp serves exactly one model; normalize the name for it
        payload["model"] = BONSAI_MODEL
    else:
        payload["model"] = await _resolve_backend_model(base, model)

    url = f"{base}/v1/chat/completions"
    if bool(payload.get("stream")):
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=5.0))
        try:
            upstream = await client.send(
                client.build_request("POST", url, json=payload), stream=True
            )
        except httpx.HTTPError as e:
            await client.aclose()
            raise HTTPException(502, f"inference backend error: {e}") from e

        async def _relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _relay(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    async with httpx.AsyncClient(timeout=300.0) as c:
        try:
            r = await c.post(url, json=payload)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"inference backend error: {e}") from e
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )


@app.get("/v1/models")
async def openai_models():
    """Aggregate models across local OpenAI-compatible backends."""
    import httpx

    models = []
    seen = set()
    backends = []
    if _state.bonsai:
        backends.append(("bonsai", BONSAI_URL))
    if _state.vllm:
        backends.append(("vllm", VLLM_URL))
    if _state.llamacpp:
        backends.append(("llamacpp", LLAMACPP_URL))
    if _state.ollama:
        backends.append(("ollama", OLLAMA_URL))

    async with httpx.AsyncClient(timeout=5.0) as c:
        for backend, base in backends:
            try:
                r = await c.get(f"{base}/v1/models")
                if r.status_code != 200:
                    continue
                for entry in (r.json().get("data") or r.json().get("models") or []):
                    mid = entry.get("id") or entry.get("name") or entry.get("model")
                    if mid and mid not in seen:
                        seen.add(mid)
                        models.append({"id": mid, "object": "model", "owned_by": backend})
            except (httpx.HTTPError, ValueError):
                continue

    return {"object": "list", "data": models}


# ── Browser proxy plane (allowlisted host-service reverse proxy) ──────
app.include_router(proxy_router)


@app.get("/endpoints")
async def list_fleet_endpoints():
    """List all endpoints in this workspace's fleet (proxied from cloud)."""
    import httpx as _httpx

    if not SYNC_API_KEY:
        return {"error": "No API key configured", "endpoints": []}

    from awnode.sync import GATEWAY_URL

    try:
        async with _httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{GATEWAY_URL}/v1/endpoints",
                headers={"Authorization": f"Bearer {SYNC_API_KEY}"},
            )
            if r.status_code == 200:
                return r.json()
            return {"error": f"HTTP {r.status_code}", "endpoints": []}
    except Exception as e:
        return {"error": str(e), "endpoints": []}


# ── Read-Only MCP Tool Surface (Hardened Authentication) ──────────────────


def _get_enrolled_bearer_token() -> Optional[str]:
    """Read the enrolled account bearer token from ~/.aither/sync_state.json.

    Returns the token if present and valid, None otherwise.
    """
    try:
        aither_home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
        sync_state_file = aither_home / "sync_state.json"

        if not sync_state_file.exists():
            return None

        data = json.loads(sync_state_file.read_text(encoding="utf-8"))
        return data.get("tenant_scoped_token") or data.get("api_token") or data.get("token")
    except Exception as e:
        logger.debug(f"Failed to read enrolled token from sync_state.json: {e}")
        return None


def _is_loopback_client(request) -> bool:
    """Check if the direct client connection is from loopback.

    Uses request.client.host (the direct peer), NOT X-Forwarded-For,
    because X-Forwarded-For is trivially spoofable by non-browser clients.
    """
    if not request.client:
        return False

    client_host = request.client.host
    # IPv4 loopback
    if client_host == "127.0.0.1":
        return True
    # IPv6 loopback
    if client_host in ("::1", "localhost"):
        return True
    return False


def _check_file_access_auth(request) -> bool:
    """Gate filesystem access with non-spoofable authentication.

    Rules:
    1. Loopback clients (127.0.0.1, ::1) are always allowed (dev convenience).
    2. Non-loopback clients must present a valid Bearer token (enrolled account).
    3. Origin header stays as defense-in-depth but is NOT the sole control.

    Returns True if access is granted, False otherwise.
    """
    # Loopback is always permitted
    if _is_loopback_client(request):
        return True

    # Non-loopback: require enrolled bearer token
    auth_header = request.headers.get("authorization", "").strip()
    if not auth_header:
        return False

    # Parse "Bearer <token>"
    if not auth_header.lower().startswith("bearer "):
        return False

    presented_token = auth_header[7:].strip()
    enrolled_token = _get_enrolled_bearer_token()

    # Enrolled token must exist and match (constant-time comparison)
    if not enrolled_token:
        logger.debug("No enrolled token found in sync_state.json")
        return False

    # Constant-time comparison to prevent timing attacks
    import hmac
    try:
        return hmac.compare_digest(presented_token, enrolled_token)
    except Exception as e:
        logger.debug(f"Bearer token comparison failed: {e}")
        return False


def _check_origin(request) -> bool:
    """Verify the request Origin is from aitherium.com family or localhost.

    This is defense-in-depth — browsers enforce CORS, so a spoofed Origin
    indicates a non-browser client. File tools already gate on bearer token
    for non-loopback, so origin mismatch is a second signal.
    """
    origin = request.headers.get("origin", "").lower()
    if not origin:
        return False

    parsed = urlparse(origin)
    hostname = parsed.hostname or ""

    # Allow aitherium.com and its subdomains
    if hostname.endswith("aitherium.com") or hostname == "aitherium.com":
        return True

    # Allow localhost variants
    if hostname in ("localhost", "127.0.0.1", "::1", ""):
        return True

    return False


def _normalize_path(requested_path: str):
    """Resolve and validate a path against the allowed root.

    Returns (resolved_path, is_valid) tuple.
    - resolved_path: The absolute path after symlink resolution
    - is_valid: True if the path is within the allowed root

    Uses AITHERNODE_FS_ROOT (default: ~/AitherShared).
    Symlink escapes and parent-directory traversals are blocked.
    """
    fs_root_path = AITHERNODE_FS_ROOT.resolve()

    try:
        # Resolve the requested path
        if requested_path.startswith("/") or requested_path.startswith("\\"):
            requested = Path(requested_path)
        else:
            requested = Path(fs_root_path) / requested_path

        # Resolve symlinks to detect escape attempts
        resolved = requested.resolve()

        # Verify resolved path is under the root
        try:
            resolved.relative_to(fs_root_path)
            return resolved, True
        except ValueError:
            # Path escapes the allowed root
            return resolved, False
    except Exception:
        return Path(requested_path), False


class MCPToolRequest(BaseModel):
    tool: str
    arguments: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None


@app.get("/mcp/tools")
async def mcp_tools_catalog(request: Request):
    """Catalog of available read-only MCP tools exposed by this node.

    File tools (read_file, list_dir) require non-spoofable authentication.
    Web search requires origin check only.
    """
    # Check if client has file access (determines which tools to advertise)
    file_access_granted = _check_file_access_auth(request)
    origin_ok = _check_origin(request)

    # Browser drive-by defense: a foreign Origin (browser cross-origin call) is denied
    # outright so the catalog — including the fs_root path — never leaks to another site.
    if bool(request.headers.get("origin", "").strip()) and not origin_ok:
        raise HTTPException(403, "Access denied: cross-origin browser request not permitted")

    # If neither file auth nor origin check passed, deny everything
    if not file_access_granted and not origin_ok:
        raise HTTPException(403, "Access denied")

    tools = {}

    # File tools only if file access auth is granted
    if file_access_granted:
        tools["read_file"] = {
            "available": True,
            "description": "Read the contents of a text file (read-only, confined to AitherShared)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "offset": {"type": "integer", "description": "Starting line number (0-based)"},
                    "limit": {"type": "integer", "description": "Maximum lines to return (0 = all)"},
                },
                "required": ["path"],
            },
        }
        tools["list_dir"] = {
            "available": True,
            "description": "List files and subdirectories in a directory (read-only, confined to AitherShared)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list"},
                    "pattern": {"type": "string", "description": "Glob pattern to filter (default: *)"},
                    "recursive": {"type": "boolean", "description": "List recursively"},
                },
                "required": ["path"],
            },
        }

    # Web search if origin is OK
    if origin_ok:
        tools["web_search"] = {
            "available": True,
            "description": "Search the web using DuckDuckGo",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default: 5)"},
                },
                "required": ["query"],
            },
        }

    return {
        "mode": "read-only",
        "node_id": os.environ.get("AITHER_NODE_ID", "consumer-node"),
        "fs_root": str(AITHERNODE_FS_ROOT.resolve()),
        "tools": tools,
    }


@app.post("/mcp/execute")
async def mcp_execute(tool_req: MCPToolRequest, http_request: Request):
    """Execute a read-only MCP tool (read_file, list_dir, web_search only).

    File tools (read_file, list_dir):
      - Gate on NON-SPOOFABLE authentication (loopback + bearer token for non-loopback)
      - Origin header is defense-in-depth but not the sole control

    Web search:
      - Lighter gate (origin-only, no filesystem risk)
    """
    tool_name = tool_req.tool
    arguments = tool_req.arguments or {}

    # File tools: require non-spoofable auth
    if tool_name in ("read_file", "list_dir"):
        if not _check_file_access_auth(http_request):
            raise HTTPException(403, "Access denied: loopback or valid bearer token required")
        # Browser drive-by defense. A request carrying an Origin header IS a browser
        # request; loopback auth passes for EVERY local browser, so a foreign Origin
        # here means another site is calling us cross-origin — deny it (CORS should
        # already have blocked the preflight; this backstops non-preflighted paths).
        # Origin-less callers (local CLI tools, curl) fall through the loopback/bearer
        # gate as before.
        origin_present = bool(http_request.headers.get("origin", "").strip())
        if origin_present and not _check_origin(http_request):
            logger.warning(
                "Denied file access: cross-origin browser request from %s (origin %r)",
                http_request.client.host if http_request.client else "?",
                http_request.headers.get("origin", ""),
            )
            raise HTTPException(403, "Access denied: cross-origin browser request not permitted")

    # Web search: lighter gate (origin-only)
    elif tool_name == "web_search":
        if not _check_origin(http_request):
            raise HTTPException(403, "Origin not permitted")

    # Whitelist check — only allow read-only tools
    if tool_name == "read_file":
        return await _execute_read_file(arguments)
    elif tool_name == "list_dir":
        return await _execute_list_dir(arguments)
    elif tool_name == "web_search":
        return await _execute_web_search(arguments)
    else:
        return {
            "success": False,
            "result": "",
            "error": f"Tool '{tool_name}' not available. Allowed tools: read_file, list_dir, web_search",
            "artifacts": [],
        }


async def _execute_read_file(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute read_file tool with path jail enforcement."""
    path = arguments.get("path", "")
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 0)

    if not path:
        return {
            "success": False,
            "result": "",
            "error": "Missing required parameter: path",
            "artifacts": [],
        }

    # Path jail check
    resolved, is_valid = _normalize_path(path)
    if not is_valid:
        return {
            "success": False,
            "result": "",
            "error": "Access denied: path escapes allowed root",
            "artifacts": [],
        }

    if not resolved.exists():
        return {
            "success": False,
            "result": "",
            "error": f"File not found: {path}",
            "artifacts": [],
        }

    if not resolved.is_file():
        return {
            "success": False,
            "result": "",
            "error": f"Not a file: {path}",
            "artifacts": [],
        }

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return {
            "success": False,
            "result": "",
            "error": f"Permission denied: {path}",
            "artifacts": [],
        }
    except Exception as e:
        return {
            "success": False,
            "result": "",
            "error": f"Failed to read file: {e}",
            "artifacts": [],
        }

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    if offset > 0 or limit > 0:
        start = min(offset, total_lines)
        end = start + limit if limit > 0 else total_lines
        lines = lines[start:end]

    content = "".join(lines)

    result = json.dumps({
        "path": str(resolved),
        "content": content,
        "total_lines": total_lines,
        "lines_returned": len(lines),
        "offset": offset,
    })

    return {
        "success": True,
        "result": result,
        "artifacts": [],
    }


async def _execute_list_dir(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute list_dir tool with path jail enforcement."""
    path = arguments.get("path", ".")
    pattern = arguments.get("pattern", "*")
    recursive = arguments.get("recursive", False)

    # Path jail check
    resolved, is_valid = _normalize_path(path)
    if not is_valid:
        return {
            "success": False,
            "result": "",
            "error": "Access denied: path escapes allowed root",
            "artifacts": [],
        }

    if not resolved.exists():
        return {
            "success": False,
            "result": "",
            "error": f"Directory not found: {path}",
            "artifacts": [],
        }

    if not resolved.is_dir():
        return {
            "success": False,
            "result": "",
            "error": f"Not a directory: {path}",
            "artifacts": [],
        }

    try:
        if recursive:
            entries = list(resolved.rglob(pattern))
        else:
            entries = list(resolved.glob(pattern))

        files = []
        dirs = []

        for entry in sorted(entries):
            rel = str(entry.relative_to(resolved))
            if entry.is_file():
                files.append({
                    "name": entry.name,
                    "path": rel,
                    "size": entry.stat().st_size,
                })
            elif entry.is_dir():
                dirs.append({
                    "name": entry.name,
                    "path": rel,
                })

        result = json.dumps({
            "path": str(resolved),
            "pattern": pattern,
            "recursive": recursive,
            "files": files,
            "directories": dirs,
            "total": len(files) + len(dirs),
        })

        return {
            "success": True,
            "result": result,
            "artifacts": [],
        }
    except Exception as e:
        return {
            "success": False,
            "result": "",
            "error": f"Failed to list directory: {e}",
            "artifacts": [],
        }


async def _execute_web_search(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Execute web_search tool using DuckDuckGo."""
    query = arguments.get("query", "")
    limit = arguments.get("limit", 5)

    if not query:
        return {
            "success": False,
            "result": "",
            "error": "Missing required parameter: query",
            "artifacts": [],
        }

    try:
        import httpx
        import re

        # Query DuckDuckGo HTML endpoint
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "awnode/1.0"},
            )
            resp.raise_for_status()
            text = resp.text

        # Parse results from HTML (simple regex extraction)
        results = []
        links = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', text)
        snippets = re.findall(r'class="result__snippet">(.*?)</(?:div|a)', text, re.DOTALL)

        for i, (url, title) in enumerate(links[:limit]):
            snippet = snippets[i].strip() if i < len(snippets) else ""
            # Clean HTML tags
            title = re.sub(r'<[^>]+>', '', title).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()

            # Decode DuckDuckGo redirect URL if needed
            if "uddg=" in url:
                try:
                    from urllib.parse import unquote, parse_qs, urlparse
                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    if "uddg=" in params:
                        url = unquote(params["uddg"][0])
                except Exception:
                    pass

            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
            })

        result = json.dumps({
            "query": query,
            "results": results,
            "count": len(results),
        })

        return {
            "success": True,
            "result": result,
            "artifacts": [],
        }
    except Exception as e:
        return {
            "success": False,
            "result": "",
            "error": f"Web search failed: {e}",
            "artifacts": [],
        }
