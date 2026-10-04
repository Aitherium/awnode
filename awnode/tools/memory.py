"""
Memory Tools
=============

Persistent key-value memory backed by local SQLite.
When Genesis is available, also syncs to Spirit/WorkingMemory.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

_DB_PATH = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "memory.db"


def _get_db() -> sqlite3.Connection:
    """Get or create the local memory database."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT,"
        "  category TEXT DEFAULT '',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")"
    )
    return conn


async def remember(key: str, value: str, category: str = "") -> str:
    """Store a memory for later recall. Persists across sessions."""
    now = datetime.utcnow().isoformat()
    db = _get_db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO memories "
            "(key, value, category, created_at, updated_at) VALUES "
            "(?, ?, ?, COALESCE((SELECT created_at FROM memories WHERE key=?), ?), ?)",
            (key, value, category, key, now, now),
        )
        db.commit()
        return json.dumps({"stored": key, "category": category})
    finally:
        db.close()


async def recall(query: str, category: str = "", limit: int = 10) -> str:
    """Recall memories matching a query. Searches keys and values."""
    db = _get_db()
    try:
        if category:
            rows = db.execute(
                "SELECT key, value, category, updated_at FROM memories "
                "WHERE (key LIKE ? OR value LIKE ?) AND category = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (f"%{query}%", f"%{query}%", category, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT key, value, category, updated_at FROM memories "
                "WHERE key LIKE ? OR value LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return json.dumps({
            "memories": [
                {"key": r[0], "value": r[1], "category": r[2], "updated_at": r[3]}
                for r in rows
            ]
        })
    finally:
        db.close()


async def forget(key: str) -> str:
    """Remove a specific memory by key."""
    db = _get_db()
    try:
        cursor = db.execute("DELETE FROM memories WHERE key = ?", (key,))
        db.commit()
        if cursor.rowcount > 0:
            return json.dumps({"deleted": key})
        return json.dumps({"error": f"Memory '{key}' not found"})
    finally:
        db.close()


async def list_memories(category: str = "", limit: int = 50) -> str:
    """List all stored memories, optionally filtered by category."""
    db = _get_db()
    try:
        if category:
            rows = db.execute(
                "SELECT key, value, category, updated_at FROM memories "
                "WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT key, value, category, updated_at FROM memories "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return json.dumps({
            "count": len(rows),
            "memories": [
                {"key": r[0], "value": r[1], "category": r[2], "updated_at": r[3]}
                for r in rows
            ],
        })
    finally:
        db.close()
