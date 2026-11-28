"""
Image generation tools -- routes to local Canvas extension or Elysium cloud.

Detects local Canvas (port 8108) or ComfyUI (port 8188) first.
Falls back to cloud generation via gateway.aitherium.com.
"""

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

CANVAS_URL = os.environ.get("AITHER_CANVAS_URL", "http://localhost:8108")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188")
CLOUD_URL = os.environ.get("AITHER_CLOUD_URL", "https://gateway.aitherium.com")
API_KEY = os.environ.get("AITHER_API_KEY", "")
OUTPUT_DIR = Path(
    os.environ.get("AITHER_OUTPUT_DIR", str(Path.home() / ".aither" / "generated"))
)


async def _detect_backend() -> str:
    """Detect available image generation backend.

    Returns:
        Backend name: "canvas", "comfyui", "cloud", or "none".
    """
    # Try Canvas first (full pipeline)
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{CANVAS_URL}/health")
            if r.status_code == 200:
                return "canvas"
    except Exception:
        pass

    # Try raw ComfyUI
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{COMFYUI_URL}/")
            if r.status_code == 200:
                return "comfyui"
    except Exception:
        pass

    # Cloud fallback
    if API_KEY:
        return "cloud"

    return "none"


def _save_image(image_b64: str, prefix: str = "gen") -> str:
    """Save base64 image to output dir, return path.

    Args:
        image_b64: Base64-encoded PNG image data.
        prefix: Filename prefix for organizing outputs.

    Returns:
        Absolute path to the saved image file.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"aither_{prefix}_{int(time.time())}.png"
    filepath = OUTPUT_DIR / filename
    filepath.write_bytes(base64.b64decode(image_b64))
    return str(filepath)


async def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = 1024,
    height: int = 1024,
    steps: int = 20,
) -> str:
    """Generate an image from a text prompt. Routes to local Canvas, ComfyUI, or cloud.

    Args:
        prompt: Detailed description of the image to generate.
        negative_prompt: What to avoid in the image.
        width: Image width in pixels (default: 1024).
        height: Image height in pixels (default: 1024).
        steps: Number of diffusion steps (default: 20).
    """
    backend = await _detect_backend()

    if backend == "canvas":
        try:
            async with httpx.AsyncClient(timeout=300.0) as c:
                r = await c.post(f"{CANVAS_URL}/generate", json={
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "steps": steps,
                })
                if r.status_code == 200:
                    data = r.json()
                    if data.get("image"):
                        path = _save_image(data["image"], "canvas")
                        return json.dumps({
                            "status": "ok", "backend": "canvas",
                            "path": path, "prompt": prompt,
                        })
                    return json.dumps(data)
                return json.dumps({
                    "error": f"Canvas returned HTTP {r.status_code}",
                    "body": r.text[:200],
                })
        except Exception as e:
            return json.dumps({
                "error": f"Canvas generation failed: {e}",
                "hint": "Is AitherCanvas running?",
            })

    elif backend == "cloud":
        try:
            async with httpx.AsyncClient(timeout=300.0) as c:
                r = await c.post(f"{CLOUD_URL}/v1/generate/image", json={
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "steps": steps,
                }, headers={"Authorization": f"Bearer {API_KEY}"})
                if r.status_code == 200:
                    data = r.json()
                    if data.get("image"):
                        path = _save_image(data["image"], "cloud")
                        return json.dumps({
                            "status": "ok", "backend": "cloud", "path": path,
                        })
                    return json.dumps(data)
                return json.dumps({
                    "error": f"Cloud generation returned HTTP {r.status_code}",
                })
        except Exception as e:
            return json.dumps({"error": f"Cloud generation failed: {e}"})

    else:
        return json.dumps({
            "error": "No image generation backend available.",
            "hint": (
                "Install Canvas: "
                "awnode ext install canvas && awnode ext start canvas"
            ),
            "alternatives": [
                "awnode ext install comfyui",
                "awnode ext install sd-webui",
                "Set AITHER_API_KEY for cloud generation",
            ],
        })


async def list_generation_backends() -> str:
    """List available image generation backends and their status.

    Returns:
        JSON with backend name, URL, and up/down status for Canvas,
        ComfyUI, and Cloud endpoints.
    """
    backends = []

    for name, url, health in [
        ("Canvas", CANVAS_URL, "/health"),
        ("ComfyUI", COMFYUI_URL, "/"),
        ("Cloud", CLOUD_URL, "/health"),
    ]:
        status = "down"
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                headers = (
                    {"Authorization": f"Bearer {API_KEY}"}
                    if "aitherium" in url else {}
                )
                r = await c.get(f"{url.rstrip('/')}{health}", headers=headers)
                status = "up" if r.status_code == 200 else f"http_{r.status_code}"
        except Exception:
            pass
        backends.append({"name": name, "url": url, "status": status})

    return json.dumps({"backends": backends}, indent=2)
