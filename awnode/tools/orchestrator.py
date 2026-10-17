"""
Orchestrator Tools
===================

Chat and agent orchestration that routes to Genesis when available,
or falls back to direct Ollama for standalone operation.
"""

import json
import os
import uuid

GENESIS_URL = os.environ.get("AITHER_URL", "http://localhost:8001")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
API_KEY = os.environ.get("AITHER_API_KEY", "")


async def _genesis_available() -> bool:
    """Quick probe for Genesis."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{GENESIS_URL}/health")
            return r.status_code == 200
    except (httpx.HTTPError, OSError, RuntimeError):
        return False


async def chat(
    message: str,
    agent: str = "",
    model: str = "",
    conversation_id: str = "",
) -> str:
    """Chat with an AI agent. Routes to Genesis if available, falls back to local Ollama."""
    import httpx

    if not message.strip():
        return json.dumps({"error": "Empty message"})

    conv_id = conversation_id or uuid.uuid4().hex[:12]

    # Try Genesis first
    if await _genesis_available():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                headers = {}
                if API_KEY:
                    headers["Authorization"] = f"Bearer {API_KEY}"
                payload = {
                    "message": message,
                    "conversation_id": conv_id,
                }
                if agent:
                    payload["agent"] = agent
                if model:
                    payload["model"] = model

                resp = await client.post(
                    f"{GENESIS_URL}/chat",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    data["via"] = "genesis"
                    data["conversation_id"] = conv_id
                    return json.dumps(data)
        except (OSError, ValueError, KeyError):
            pass  # Fall through to Ollama

    # Fallback to Ollama
    try:
        ollama_model = model or "llama3.2:3b"
        messages = []
        if agent:
            messages.append({
                "role": "system",
                "content": f"You are {agent}, an AI assistant. Be helpful and concise.",
            })
        messages.append({"role": "user", "content": message})

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": ollama_model, "messages": messages, "stream": False},
            )
            if resp.status_code == 200:
                data = resp.json()
                return json.dumps({
                    "response": data.get("message", {}).get("content", ""),
                    "model_used": data.get("model", ollama_model),
                    "conversation_id": conv_id,
                    "via": "ollama",
                    "metadata": {
                        "eval_count": data.get("eval_count", 0),
                        "total_duration_ms": data.get("total_duration", 0) // 1_000_000,
                    },
                })
    except (OSError, ValueError, KeyError):
        pass

    return json.dumps({
        "error": "No LLM backend available. Start Genesis or install Ollama.",
        "hint": "Run: ollama serve && ollama pull llama3.2:3b",
    })


async def ask_agent(question: str, agent: str = "aither") -> str:
    """Ask a specific agent a question. Routes to Genesis for full agent capabilities."""
    import httpx

    if await _genesis_available():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                headers = {}
                if API_KEY:
                    headers["Authorization"] = f"Bearer {API_KEY}"
                resp = await client.post(
                    f"{GENESIS_URL}/forge/dispatch/sync",
                    json={
                        "agent": agent,
                        "task": question,
                        "parent_agent": "system",
                    },
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    data["via"] = "genesis-forge"
                    return json.dumps(data)
        except (OSError, ValueError, KeyError):
            pass

    # Fallback to basic Ollama chat with agent persona
    return await chat(message=question, agent=agent)


async def list_agents() -> str:
    """List available agents from Genesis, or return built-in defaults."""
    import httpx

    if await _genesis_available():
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                headers = {}
                if API_KEY:
                    headers["Authorization"] = f"Bearer {API_KEY}"
                resp = await client.get(
                    f"{GENESIS_URL}/agents/list",
                    headers=headers,
                )
                if resp.status_code == 200:
                    return json.dumps(resp.json())
        except (OSError, ValueError, KeyError):
            pass

    # Offline fallback — common agent names
    return json.dumps({
        "agents": [
            {"name": "aither", "role": "General assistant"},
            {"name": "demiurge", "role": "Code generation and architecture"},
            {"name": "hydra", "role": "Code review and testing"},
            {"name": "athena", "role": "Security analysis"},
            {"name": "atlas", "role": "Service discovery and infrastructure"},
            {"name": "apollo", "role": "Performance optimization"},
            {"name": "prometheus", "role": "Simulation and worldbuilding"},
            {"name": "scribe", "role": "Documentation"},
        ],
        "via": "offline-defaults",
    })
