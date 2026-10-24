"""
Mermaid Diagram Tools
======================

Generate Mermaid diagram syntax from descriptions.
Uses the local llama.cpp server for intelligent generation, or a template fallback.
"""

import json
import logging

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

    # Try LLM-powered generation via the local llama.cpp server
    from awnode.tools import local_chat
    try:
        prompt = (
            f"Generate a Mermaid {diagram_type} diagram for the following description. "
            f"Return ONLY the Mermaid code, no explanation, no markdown fences.\n\n"
            f"Description: {description}\n\n"
            f"The diagram MUST start with '{_DIAGRAM_TYPES[diagram_type]}'."
        )
        out = await local_chat([{"role": "user", "content": prompt}], timeout=60.0)
        mermaid_code = out["content"].strip()
        # Strip markdown fences if the model added them
        if mermaid_code.startswith("```"):
            lines = mermaid_code.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            mermaid_code = "\n".join(lines).strip()
        if mermaid_code:
            return json.dumps({
                "mermaid": mermaid_code,
                "type": diagram_type,
                "method": "llamacpp",
            })
    except Exception as exc:  # noqa: BLE001 - any backend failure falls back to the template
        logging.getLogger(__name__).debug("local llama.cpp diagram generation failed: %s", exc)

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
        "hint": "Run a local llama.cpp server (LLAMACPP_URL) for intelligent diagrams.",
    })


async def list_diagram_types() -> str:
    """List all supported Mermaid diagram types."""
    return json.dumps({
        "types": [
            {"name": k, "header": v}
            for k, v in _DIAGRAM_TYPES.items()
        ]
    })
