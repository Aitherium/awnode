"""
Ollama Tools
=============

Manage and interact with locally installed Ollama models.
Requires Ollama to be running (default: http://localhost:11434).
"""

import json
import os

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


async def _ollama_available() -> bool:
    """Check if Ollama is reachable."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            return r.status_code == 200
    except (httpx.HTTPError, OSError, RuntimeError):
        return False


async def ollama_list_models() -> str:
    """List all locally installed Ollama models."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code != 200:
                return json.dumps({"error": f"Ollama returned HTTP {resp.status_code}"})
            data = resp.json()
            models = []
            for m in data.get("models", []):
                models.append({
                    "name": m.get("name", ""),
                    "size": m.get("size", 0),
                    "modified_at": m.get("modified_at", ""),
                    "family": m.get("details", {}).get("family", ""),
                    "parameter_size": m.get("details", {}).get("parameter_size", ""),
                    "quantization": m.get("details", {}).get("quantization_level", ""),
                })
            return json.dumps({"models": models, "count": len(models)})
    except Exception as e:
        return json.dumps({
            "error": f"Cannot connect to Ollama at {OLLAMA_URL}: {e}",
            "hint": "Is Ollama running? Start it with: ollama serve",
        })


async def ollama_pull_model(model: str) -> str:
    """Pull/download an Ollama model."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/pull",
                json={"name": model, "stream": False},
            )
            if resp.status_code == 200:
                return json.dumps({"pulled": model, "status": "success"})
            return json.dumps({"error": f"Pull failed with HTTP {resp.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"Pull failed: {e}"})


async def ollama_chat(
    message: str,
    model: str = "llama3.2:3b",
    system: str = "",
) -> str:
    """Chat with a local Ollama model."""
    import httpx
    try:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": message})

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": model, "messages": messages, "stream": False},
            )
            if resp.status_code != 200:
                return json.dumps({"error": f"Ollama returned HTTP {resp.status_code}"})

            data = resp.json()
            return json.dumps({
                "response": data.get("message", {}).get("content", ""),
                "model": data.get("model", model),
                "done": data.get("done", False),
                "total_duration_ms": data.get("total_duration", 0) // 1_000_000,
                "eval_count": data.get("eval_count", 0),
            })
    except Exception as e:
        return json.dumps({
            "error": f"Ollama chat failed: {e}",
            "hint": f"Is model '{model}' installed? Run: ollama pull {model}",
        })


async def ollama_generate(prompt: str, model: str = "llama3.2:3b") -> str:
    """Generate text completion using local Ollama."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
            if resp.status_code != 200:
                return json.dumps({"error": f"Ollama returned HTTP {resp.status_code}"})

            data = resp.json()
            return json.dumps({
                "response": data.get("response", ""),
                "model": data.get("model", model),
                "done": data.get("done", False),
                "total_duration_ms": data.get("total_duration", 0) // 1_000_000,
                "eval_count": data.get("eval_count", 0),
            })
    except Exception as e:
        return json.dumps({
            "error": f"Ollama generate failed: {e}",
            "hint": f"Is model '{model}' installed? Run: ollama pull {model}",
        })
