"""
Search Tools — Code and File Search
=====================================

Text search, regex search, and file discovery using only the Python
standard library. Works fully offline with no platform dependencies.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _get_root() -> Path:
    """Get the search root directory."""
    root = os.environ.get("AITHER_ROOT")
    if root:
        return Path(root)
    return Path.cwd()


# Default binary extensions to skip during content search
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".pyc", ".pyo", ".whl", ".egg",
    ".woff", ".woff2", ".ttf", ".eot",
    ".db", ".sqlite", ".sqlite3",
})

# Directories to skip during recursive search
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".next", ".nuxt", "dist", "build", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "coverage", ".eggs",
})


def _is_text_file(path: Path) -> bool:
    """Check if a file is likely a text file based on extension."""
    return path.suffix.lower() not in _BINARY_EXTENSIONS


def _should_skip_dir(name: str) -> bool:
    """Check if a directory should be skipped during search."""
    return name in _SKIP_DIRS


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def grep_search(
    pattern: str,
    path: str = ".",
    file_pattern: str = "",
    max_results: int = 50,
    case_sensitive: bool = True,
) -> str:
    """Search for a text pattern in files (like grep/ripgrep).

    Searches file contents for lines matching the given pattern. Supports
    plain text and regex patterns. Skips binary files and common build
    directories automatically.

    Args:
        pattern: Text or regex pattern to search for.
        path: Directory or file to search in (default: current directory).
        file_pattern: Glob pattern to filter files (e.g., "*.py", "*.ts").
        max_results: Maximum number of matches to return (default: 50).
        case_sensitive: Whether the search is case-sensitive (default: True).

    Returns:
        JSON string with list of matches (file, line number, content).
    """
    root = _get_root()
    search_path = root / path if not Path(path).is_absolute() else Path(path)

    if not search_path.exists():
        return json.dumps({"error": f"Path not found: {search_path}"})

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex pattern: {e}"})

    matches = []
    files_searched = 0

    def _search_file(filepath: Path) -> None:
        nonlocal files_searched
        if not _is_text_file(filepath):
            return
        files_searched += 1
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except (PermissionError, OSError):
            return

        for line_num, line in enumerate(text.splitlines(), 1):
            if len(matches) >= max_results:
                return
            if compiled.search(line):
                try:
                    rel = str(filepath.relative_to(root))
                except ValueError:
                    rel = str(filepath)
                matches.append({
                    "file": rel,
                    "line": line_num,
                    "content": line.rstrip()[:300],
                })

    if search_path.is_file():
        _search_file(search_path)
    else:
        for dirpath, dirnames, filenames in os.walk(str(search_path)):
            # Prune skipped directories
            dirnames[:] = [
                d for d in dirnames if not _should_skip_dir(d)
            ]

            for fname in filenames:
                if len(matches) >= max_results:
                    break
                filepath = Path(dirpath) / fname
                if file_pattern and not filepath.match(file_pattern):
                    continue
                _search_file(filepath)

    result = {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "files_searched": files_searched,
    }
    if len(matches) >= max_results:
        result["truncated"] = True
        result["note"] = f"Results capped at {max_results}. Use a more specific pattern."

    return json.dumps(result, indent=2)


def find_files(
    pattern: str = "*",
    path: str = ".",
    file_type: str = "",
    max_results: int = 100,
) -> str:
    """Find files matching a glob pattern.

    Recursively searches for files matching the given glob pattern.
    Automatically skips common build directories (.git, node_modules, etc.).

    Args:
        pattern: Glob pattern (e.g., "*.py", "test_*.py", "**/*.tsx").
        path: Directory to search in (default: current directory).
        file_type: Filter by extension without dot (e.g., "py", "ts", "rs").
        max_results: Maximum number of files to return (default: 100).

    Returns:
        JSON string with list of matching file paths and sizes.
    """
    root = _get_root()
    search_path = root / path if not Path(path).is_absolute() else Path(path)

    if not search_path.exists():
        return json.dumps({"error": f"Path not found: {search_path}"})

    if not search_path.is_dir():
        return json.dumps({"error": f"Not a directory: {search_path}"})

    files = []

    for dirpath, dirnames, filenames in os.walk(str(search_path)):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for fname in filenames:
            if len(files) >= max_results:
                break

            filepath = Path(dirpath) / fname

            # Check glob pattern
            if pattern != "*" and not filepath.match(pattern):
                continue

            # Check file type filter
            if file_type and filepath.suffix.lstrip(".").lower() != file_type.lower():
                continue

            try:
                rel = str(filepath.relative_to(root))
            except ValueError:
                rel = str(filepath)

            try:
                size = filepath.stat().st_size
            except OSError:
                size = -1

            files.append({"path": rel, "size": size})

    result = {
        "pattern": pattern,
        "files": files,
        "count": len(files),
    }
    if len(files) >= max_results:
        result["truncated"] = True

    return json.dumps(result, indent=2)


def regex_search(
    pattern: str,
    path: str = ".",
    file_pattern: str = "",
    context_lines: int = 0,
    max_results: int = 30,
) -> str:
    """Search files using a regex pattern with optional context lines.

    Like grep_search but returns surrounding context lines for each match,
    making it easier to understand matches in context.

    Args:
        pattern: Regular expression pattern to search for.
        path: Directory or file to search in.
        file_pattern: Glob to filter files (e.g., "*.py").
        context_lines: Number of lines to show before and after each match.
        max_results: Maximum number of matches to return (default: 30).

    Returns:
        JSON string with matches including context lines.
    """
    root = _get_root()
    search_path = root / path if not Path(path).is_absolute() else Path(path)

    if not search_path.exists():
        return json.dumps({"error": f"Path not found: {search_path}"})

    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex: {e}"})

    matches = []
    files_searched = 0

    def _search_file(filepath: Path) -> None:
        nonlocal files_searched
        if not _is_text_file(filepath):
            return
        files_searched += 1
        try:
            lines = filepath.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except (PermissionError, OSError):
            return

        for i, line in enumerate(lines):
            if len(matches) >= max_results:
                return
            if compiled.search(line):
                try:
                    rel = str(filepath.relative_to(root))
                except ValueError:
                    rel = str(filepath)

                match_entry = {
                    "file": rel,
                    "line": i + 1,
                    "content": line.rstrip()[:300],
                }

                if context_lines > 0:
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    ctx = []
                    for j in range(start, end):
                        prefix = ">" if j == i else " "
                        ctx.append(f"{prefix} {j + 1}: {lines[j].rstrip()[:200]}")
                    match_entry["context"] = "\n".join(ctx)

                matches.append(match_entry)

    if search_path.is_file():
        _search_file(search_path)
    else:
        for dirpath, dirnames, filenames in os.walk(str(search_path)):
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
            for fname in filenames:
                if len(matches) >= max_results:
                    break
                filepath = Path(dirpath) / fname
                if file_pattern and not filepath.match(file_pattern):
                    continue
                _search_file(filepath)

    result = {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "files_searched": files_searched,
    }
    if len(matches) >= max_results:
        result["truncated"] = True

    return json.dumps(result, indent=2)


__all__ = [
    "grep_search",
    "find_files",
    "regex_search",
]
