"""
Filesystem Tools — File Operations
====================================

Read, write, list, and manage files on the local filesystem.
Works fully offline with no platform dependencies.

Paths are resolved relative to the current working directory or
the ``AITHER_ROOT`` environment variable if set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def _resolve_path(path: str) -> Path:
    """Resolve a path relative to AITHER_ROOT or cwd.

    Args:
        path: Absolute or relative file path.

    Returns:
        Resolved absolute Path.
    """
    p = Path(path)
    if p.is_absolute():
        return p
    root = os.environ.get("AITHER_ROOT")
    if root:
        return Path(root) / p
    return Path.cwd() / p


def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read the contents of a file.

    Reads a text file and returns its contents. Supports reading a subset
    of lines via offset and limit parameters for large files.

    Args:
        path: Path to the file (absolute or relative to AITHER_ROOT/cwd).
        offset: Line number to start reading from (0-based, default: 0).
        limit: Maximum number of lines to return (0 = all lines).

    Returns:
        JSON string with file contents, line count, and path.
    """
    resolved = _resolve_path(path)

    if not resolved.exists():
        return json.dumps({"error": f"File not found: {resolved}"})

    if not resolved.is_file():
        return json.dumps({"error": f"Not a file: {resolved}"})

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return json.dumps({"error": f"Permission denied: {resolved}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to read {resolved}: {e}"})

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    if offset > 0 or limit > 0:
        start = min(offset, total_lines)
        end = start + limit if limit > 0 else total_lines
        lines = lines[start:end]

    content = "".join(lines)

    return json.dumps({
        "path": str(resolved),
        "content": content,
        "total_lines": total_lines,
        "lines_returned": len(lines),
        "offset": offset,
    })


def write_file(path: str, content: str, create_dirs: bool = True) -> str:
    """Write content to a file, creating parent directories if needed.

    Overwrites the file if it already exists. Use ``create_dirs=True``
    (default) to automatically create missing parent directories.

    Args:
        path: Path to the file (absolute or relative to AITHER_ROOT/cwd).
        content: Text content to write.
        create_dirs: Create parent directories if they do not exist.

    Returns:
        JSON string with success status and file info.
    """
    resolved = _resolve_path(path)

    try:
        if create_dirs:
            resolved.parent.mkdir(parents=True, exist_ok=True)

        resolved.write_text(content, encoding="utf-8")

        return json.dumps({
            "success": True,
            "path": str(resolved),
            "size": len(content),
            "lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
        })
    except PermissionError:
        return json.dumps({"error": f"Permission denied: {resolved}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to write {resolved}: {e}"})


def list_directory(path: str = ".", pattern: str = "*", recursive: bool = False) -> str:
    """List files and directories at the given path.

    Args:
        path: Directory path (default: current directory).
        pattern: Glob pattern to filter entries (default: ``*`` for all).
        recursive: If True, list recursively (using ``**`` glob).

    Returns:
        JSON string with lists of files and directories found.
    """
    resolved = _resolve_path(path)

    if not resolved.exists():
        return json.dumps({"error": f"Directory not found: {resolved}"})

    if not resolved.is_dir():
        return json.dumps({"error": f"Not a directory: {resolved}"})

    try:
        if recursive:
            entries = list(resolved.rglob(pattern))
        else:
            entries = list(resolved.glob(pattern))

        files = []
        dirs = []

        for entry in sorted(entries):
            rel = str(entry.relative_to(resolved))
            if entry.is_file():
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = -1
                files.append({"name": rel, "size": size})
            elif entry.is_dir():
                dirs.append({"name": rel})

        # Cap output to avoid overwhelming responses
        MAX_ENTRIES = 200
        truncated = False
        if len(files) > MAX_ENTRIES:
            files = files[:MAX_ENTRIES]
            truncated = True

        result = {
            "path": str(resolved),
            "files": files,
            "directories": dirs,
            "file_count": len(files),
            "dir_count": len(dirs),
        }
        if truncated:
            result["truncated"] = True
            result["note"] = f"Output capped at {MAX_ENTRIES} files"

        return json.dumps(result, indent=2)
    except PermissionError:
        return json.dumps({"error": f"Permission denied: {resolved}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to list {resolved}: {e}"})


def file_exists(path: str) -> str:
    """Check whether a file or directory exists.

    Args:
        path: Path to check.

    Returns:
        JSON string with existence status and file type.
    """
    resolved = _resolve_path(path)
    exists = resolved.exists()

    result = {
        "path": str(resolved),
        "exists": exists,
    }
    if exists:
        result["is_file"] = resolved.is_file()
        result["is_dir"] = resolved.is_dir()
        try:
            stat = resolved.stat()
            result["size"] = stat.st_size
        except OSError:
            pass

    return json.dumps(result)


def create_directory(path: str) -> str:
    """Create a directory, including any missing parent directories.

    Args:
        path: Directory path to create.

    Returns:
        JSON string with success status.
    """
    resolved = _resolve_path(path)

    try:
        resolved.mkdir(parents=True, exist_ok=True)
        return json.dumps({
            "success": True,
            "path": str(resolved),
            "created": True,
        })
    except PermissionError:
        return json.dumps({"error": f"Permission denied: {resolved}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to create directory {resolved}: {e}"})


__all__ = [
    "read_file",
    "write_file",
    "list_directory",
    "file_exists",
    "create_directory",
]
