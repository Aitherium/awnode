"""
Mermaid Diagram Tools
======================

Generate Mermaid diagram syntax from descriptions.
Uses local Ollama for intelligent generation, or template-based fallback.
"""

import json
import os

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

_DIAGRAM_TYPES = {
    "flowchart": "flowchart TD",
    "sequence": "sequenceDiagram",
    "class": "classDiagram",
    "state": "stateDiagram-v2",
    "er": "erDiagram",
    "gantt": "gantt",
    "pie": "pie",
    "mindmap": "mindmap",
    "timeline": "timeline",
    "git": "gitGraph",
}


async def generate_mermaid(
    description: str,
    diagram_type: str = "flowchart",
) -> str:
    """Generate a Mermaid diagram from a description."""
    if not description.strip():
        return json.dumps({"error": "Empty description provided"})

    if diagram_type not in _DIAGRAM_TYPES:
        return json.dumps({
            "error": f"Unknown diagram type: {diagram_type}",
            "supported": list(_DIAGRAM_TYPES.keys()),
        })

    # Try LLM-powered generation via Ollama
    import httpx
    try:
        prompt = (
            f"Generate a Mermaid {diagram_type} diagram for the following description. "
            f"Return ONLY the Mermaid code, no explanation, no markdown fences.\n\n"
            f"Description: {description}\n\n"
            f"The diagram MUST start with '{_DIAGRAM_TYPES[diagram_type]}'."
        )
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": "llama3.2:3b", "prompt": prompt, "stream": False},
            )
            if resp.status_code == 200:
                data = resp.json()
                mermaid_code = data.get("response", "").strip()
                # Strip markdown fences if the model added them
                if mermaid_code.startswith("```"):
                    lines = mermaid_code.split("\n")
                    lines = [l for l in lines if not l.startswith("```")]
                    mermaid_code = "\n".join(lines).strip()
                return json.dumps({
                    "mermaid": mermaid_code,
                    "type": diagram_type,
                    "method": "ollama",
                })
    except Exception:
        pass

    # Fallback: generate a simple template
    header = _DIAGRAM_TYPES[diagram_type]
    if diagram_type == "flowchart":
        mermaid_code = f"{header}\n    A[Start] --> B[{description[:40]}]\n    B --> C[End]"
    elif diagram_type == "sequence":
        mermaid_code = f"{header}\n    participant User\n    participant System\n    User->>System: {description[:50]}\n    System-->>User: Response"
    elif diagram_type == "pie":
        mermaid_code = f'{header}\n    title {description[:40]}\n    "Part A" : 50\n    "Part B" : 30\n    "Part C" : 20'
    elif diagram_type == "mindmap":
        mermaid_code = f"{header}\n  root(({description[:30]}))\n    Branch A\n    Branch B\n    Branch C"
    else:
        mermaid_code = f"{header}\n    %% {description}"

    return json.dumps({
        "mermaid": mermaid_code,
        "type": diagram_type,
        "method": "template",
        "hint": "Install Ollama for intelligent diagram generation.",
    })


async def list_diagram_types() -> str:
    """List all supported Mermaid diagram types."""
    return json.dumps({
        "types": [
            {"name": k, "header": v}
            for k, v in _DIAGRAM_TYPES.items()
        ]
    })
