"""
Embedding Tools
================

Generate text embeddings using local Ollama or cloud backends.
Falls back gracefully when no embedding backend is available.
"""

import json
import os

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
GENESIS_URL = os.environ.get("AITHER_URL", "http://localhost:8001")

# Default embedding models by backend
_OLLAMA_EMBED_MODEL = "nomic-embed-text"
_CLOUD_EMBED_MODEL = "text-embedding-3-small"


async def generate_embedding(text: str, model: str = "auto") -> str:
    """Generate an embedding vector for the given text. Uses local Ollama or cloud."""
    import httpx

    if not text.strip():
        return json.dumps({"error": "Empty text provided"})

    # Try Ollama first (local, free)
    if model == "auto" or model.startswith("nomic") or model.startswith("all-minilm"):
        ollama_model = model if model != "auto" else _OLLAMA_EMBED_MODEL
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/embed",
                    json={"model": ollama_model, "input": text},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    embeddings = data.get("embeddings", [])
                    if embeddings:
                        return json.dumps({
                            "embedding": embeddings[0],
                            "model": ollama_model,
                            "dimensions": len(embeddings[0]),
                            "backend": "ollama",
                        })
        except Exception:
            pass  # Fall through to next backend

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
        "error": "No embedding backend available. Install Ollama and pull "
        f"'{_OLLAMA_EMBED_MODEL}', or start Genesis.",
        "hint": f"Run: ollama pull {_OLLAMA_EMBED_MODEL}",
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
