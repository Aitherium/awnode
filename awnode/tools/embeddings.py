"""
Embedding Tools
================

Generate text embeddings using the local llama.cpp server or Genesis.
Falls back gracefully when no embedding backend is available.
"""

import json
import logging
import os

GENESIS_URL = os.environ.get("AITHER_URL", "http://localhost:8001")

# Default embedding models by backend
_LOCAL_EMBED_MODEL = "nomic-embed-text"
_CLOUD_EMBED_MODEL = "text-embedding-3-small"


async def generate_embedding(text: str, model: str = "auto") -> str:
    """Generate an embedding vector for the text: local llama.cpp server, then Genesis."""
    import httpx

    if not text.strip():
        return json.dumps({"error": "Empty text provided"})

    # Try the local OpenAI-compatible server first (llama.cpp `llama-server --embedding`)
    from awnode.tools import LOCAL_LLM_URL
    local_model = model if model != "auto" else _LOCAL_EMBED_MODEL
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{LOCAL_LLM_URL}/v1/embeddings",
                json={"model": local_model, "input": text},
            )
            if resp.status_code == 200:
                rows = resp.json().get("data", [])
                if rows and rows[0].get("embedding"):
                    vec = rows[0]["embedding"]
                    return json.dumps({
                        "embedding": vec,
                        "model": local_model,
                        "dimensions": len(vec),
                        "backend": "llamacpp",
                    })
    except Exception as exc:  # noqa: BLE001 - fall through to the next backend
        logging.getLogger(__name__).debug("local embeddings unavailable: %s", exc)

    # Try Genesis vLLM embeddings endpoint
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{GENESIS_URL}/embeddings/generate",
                json={"text": text, "model": model if model != "auto" else ""},
            )
            if resp.status_code == 200:
                data = resp.json()
                embedding = data.get("embedding", [])
                if embedding:
                    return json.dumps({
                        "embedding": embedding,
                        "model": data.get("model", "genesis"),
                        "dimensions": len(embedding),
                        "backend": "genesis",
                    })
    except Exception:
        pass

    return json.dumps({
        "error": "No embedding backend available. Start a local llama.cpp server "
        "with --embedding, or start Genesis.",
        "hint": "Set LLAMACPP_URL to an OpenAI-compatible /v1/embeddings server.",
    })


async def similarity(text_a: str, text_b: str, model: str = "auto") -> str:
    """Compute cosine similarity between two texts using embeddings."""
    import httpx

    result_a = json.loads(await generate_embedding(text_a, model))
    if "error" in result_a:
        return json.dumps(result_a)

    result_b = json.loads(await generate_embedding(text_b, model))
    if "error" in result_b:
        return json.dumps(result_b)

    vec_a = result_a["embedding"]
    vec_b = result_b["embedding"]

    if len(vec_a) != len(vec_b):
        return json.dumps({"error": "Embedding dimension mismatch"})

    # Cosine similarity
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = sum(a * a for a in vec_a) ** 0.5
    mag_b = sum(b * b for b in vec_b) ** 0.5

    if mag_a == 0 or mag_b == 0:
        return json.dumps({"similarity": 0.0, "model": result_a.get("model", "")})

    score = dot / (mag_a * mag_b)
    return json.dumps({
        "similarity": round(score, 6),
        "model": result_a.get("model", ""),
        "backend": result_a.get("backend", ""),
    })
