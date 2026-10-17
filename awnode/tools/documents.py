"""
Document Tools
===============

Read and process common document formats (txt, md, json, csv, pdf).
Summarization uses local Ollama when available.
"""

import json
import os
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


async def read_document(path: str) -> str:
    """Read a document file (txt, md, json, csv, pdf)."""
    doc_path = Path(path)
    if not doc_path.exists():
        return json.dumps({"error": f"File not found: {path}"})

    suffix = doc_path.suffix.lower()
    size = doc_path.stat().st_size

    # Guard against very large files
    max_size = 10 * 1024 * 1024  # 10 MB
    if size > max_size:
        return json.dumps({
            "error": f"File too large ({size} bytes). Max: {max_size}",
            "path": path,
        })

    try:
        if suffix == ".json":
            with open(doc_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return json.dumps({
                "path": str(doc_path),
                "type": "json",
                "size_bytes": size,
                "content": data,
            })

        elif suffix == ".csv":
            import csv as csv_mod
            with open(doc_path, "r", encoding="utf-8", newline="") as f:
                reader = csv_mod.DictReader(f)
                rows = list(reader)
            return json.dumps({
                "path": str(doc_path),
                "type": "csv",
                "size_bytes": size,
                "row_count": len(rows),
                "columns": list(rows[0].keys()) if rows else [],
                "rows": rows[:100],  # Cap preview at 100 rows
                "truncated": len(rows) > 100,
            })

        elif suffix == ".pdf":
            # Best-effort PDF text extraction
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(str(doc_path))
                pages = []
                for page in doc:
                    pages.append(page.get_text())
                doc.close()
                return json.dumps({
                    "path": str(doc_path),
                    "type": "pdf",
                    "size_bytes": size,
                    "page_count": len(pages),
                    "content": "\n\n".join(pages),
                })
            except ImportError:
                return json.dumps({
                    "path": str(doc_path),
                    "type": "pdf",
                    "size_bytes": size,
                    "error": "PDF reading requires PyMuPDF. Install: pip install pymupdf",
                })

        else:
            # Plain text / markdown / any text file
            with open(doc_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return json.dumps({
                "path": str(doc_path),
                "type": suffix.lstrip(".") or "txt",
                "size_bytes": size,
                "line_count": content.count("\n") + 1,
                "content": content,
            })

    except UnicodeDecodeError:
        return json.dumps({"error": f"Cannot read binary file as text: {path}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to read {path}: {e}"})


async def summarize_text(text: str, max_length: int = 500) -> str:
    """Create a summary of the given text (uses local LLM if available)."""
    if not text.strip():
        return json.dumps({"error": "Empty text provided"})

    # Truncate very long inputs to avoid overwhelming local models
    truncated = False
    if len(text) > 16000:
        text = text[:16000]
        truncated = True

    # Try Ollama for LLM-powered summarization
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "llama3.2:3b",
                    "prompt": (
                        f"Summarize the following text in {max_length} characters or less. "
                        f"Be concise and capture the key points.\n\n{text}"
                    ),
                    "stream": False,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return json.dumps({
                    "summary": data.get("response", ""),
                    "method": "ollama",
                    "input_length": len(text),
                    "truncated": truncated,
                })
    except Exception:
        pass

    # Fallback: extractive summary (first and last paragraphs)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text[:max_length]]

    if len(paragraphs) <= 2:
        summary = " ".join(paragraphs)
    else:
        summary = paragraphs[0] + " [...] " + paragraphs[-1]

    if len(summary) > max_length:
        summary = summary[:max_length - 3] + "..."

    return json.dumps({
        "summary": summary,
        "method": "extractive",
        "input_length": len(text),
        "truncated": truncated,
    })
