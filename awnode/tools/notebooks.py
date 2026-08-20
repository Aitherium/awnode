"""
Notebook Tools
===============

Create and read Jupyter notebooks (.ipynb files).
Stores notebooks under AITHER_HOME/notebooks by default.
"""

import json
import os
import uuid
from pathlib import Path

_NOTEBOOKS_DIR = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "notebooks"


def _ensure_dir() -> Path:
    """Ensure the notebooks directory exists."""
    _NOTEBOOKS_DIR.mkdir(parents=True, exist_ok=True)
    return _NOTEBOOKS_DIR


def _make_notebook(cells: list) -> dict:
    """Build a minimal nbformat v4 notebook structure."""
    nb_cells = []
    for cell in cells:
        cell_type = cell.get("type", "code")
        source = cell.get("source", "")
        if isinstance(source, str):
            source = source.split("\n")
        nb_cell = {
            "cell_type": cell_type,
            "metadata": {},
            "source": source,
        }
        if cell_type == "code":
            nb_cell["execution_count"] = None
            nb_cell["outputs"] = []
        nb_cells.append(nb_cell)

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.10.0"},
        },
        "cells": nb_cells,
    }


async def create_notebook(name: str, cells: str = "[]") -> str:
    """Create a new Jupyter notebook with optional initial cells."""
    nb_dir = _ensure_dir()

    try:
        cell_list = json.loads(cells) if cells else []
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid cells JSON. Expected list of {type, source}."})

    if not cell_list:
        cell_list = [{"type": "code", "source": "# New notebook\n"}]

    notebook = _make_notebook(cell_list)

    # Sanitize name
    if not name.endswith(".ipynb"):
        name = f"{name}.ipynb"
    safe_name = name.replace("/", "_").replace("\\", "_")
    path = nb_dir / safe_name

    # Avoid overwriting
    if path.exists():
        stem = path.stem
        safe_name = f"{stem}_{uuid.uuid4().hex[:8]}.ipynb"
        path = nb_dir / safe_name

    with open(path, "w", encoding="utf-8") as f:
        json.dump(notebook, f, indent=1)

    return json.dumps({
        "created": str(path),
        "name": safe_name,
        "cell_count": len(cell_list),
    })


async def read_notebook(path: str) -> str:
    """Read and parse a Jupyter notebook file."""
    nb_path = Path(path)
    if not nb_path.exists():
        # Try under notebooks dir
        nb_path = _NOTEBOOKS_DIR / path
        if not nb_path.exists():
            return json.dumps({"error": f"Notebook not found: {path}"})

    try:
        with open(nb_path, "r", encoding="utf-8") as f:
            notebook = json.load(f)
    except json.JSONDecodeError:
        return json.dumps({"error": f"Invalid notebook format: {path}"})

    cells = []
    for cell in notebook.get("cells", []):
        source = cell.get("source", [])
        if isinstance(source, list):
            source = "".join(source)
        cells.append({
            "type": cell.get("cell_type", "code"),
            "source": source,
            "outputs": len(cell.get("outputs", [])),
        })

    kernel = notebook.get("metadata", {}).get("kernelspec", {})
    return json.dumps({
        "path": str(nb_path),
        "kernel": kernel.get("display_name", "unknown"),
        "cell_count": len(cells),
        "cells": cells,
    })


async def list_notebooks() -> str:
    """List all notebooks in the local notebooks directory."""
    nb_dir = _ensure_dir()
    notebooks = []
    for p in sorted(nb_dir.glob("*.ipynb")):
        stat = p.stat()
        notebooks.append({
            "name": p.name,
            "path": str(p),
            "size_bytes": stat.st_size,
            "modified": stat.st_mtime,
        })
    return json.dumps({"notebooks": notebooks, "count": len(notebooks)})
