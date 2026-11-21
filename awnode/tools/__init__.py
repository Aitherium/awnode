"""
awnode Standalone Tools
============================

Lightweight tool modules that work offline (shell_local scope) and get
enhanced when Genesis or cloud backends are available.

Each module exposes async functions that return JSON strings.
Local data is stored under AITHER_HOME (default: ~/.aither).
"""

from pathlib import Path
import os

AITHER_HOME = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
GENESIS_URL = os.environ.get("AITHER_URL", "http://localhost:8001")
# Local OpenAI-compatible model server (llama.cpp `llama-server`, vLLM, LM Studio).
# Same env + default as awnode.server.LLAMACPP_URL so the tools and the chat proxy
# agree on which local backend this node has.
LOCAL_LLM_URL = os.environ.get("LLAMACPP_URL", "http://localhost:8200").rstrip("/")
LOCAL_LLM_MODEL = os.environ.get("AWNODE_LOCAL_MODEL", "")


async def local_chat(messages: list, model: str = "", timeout: float = 120.0) -> dict:
    """One non-streaming chat turn against the local OpenAI-compatible server.

    Args:
        messages: OpenAI-style message list.
        model: Model id; empty lets the server use its loaded model.
        timeout: Request timeout in seconds.

    Returns:
        {"content", "model"} on success; raises httpx.HTTPError / ValueError otherwise.
    """
    import httpx
    payload = {"model": model or LOCAL_LLM_MODEL, "messages": messages, "stream": False}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{LOCAL_LLM_URL}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    return {"content": choice.get("message", {}).get("content", ""),
            "model": data.get("model", payload["model"] or "local"),
            "usage": data.get("usage", {})}
