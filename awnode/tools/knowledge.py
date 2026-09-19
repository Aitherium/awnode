"""
Knowledge Graph Tools
======================

Local SQLite-backed knowledge graph using subject-predicate-object triples.
When Genesis is available, queries can be routed to the full memory graph.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

_DB_PATH = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "knowledge.db"


def _get_db() -> sqlite3.Connection:
    """Get or create the local knowledge database."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS triples ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  subject TEXT NOT NULL,"
        "  predicate TEXT NOT NULL,"
        "  object TEXT NOT NULL,"
        "  source TEXT DEFAULT '',"
        "  confidence REAL DEFAULT 1.0,"
        "  created_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_subject ON triples(subject)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_object ON triples(object)"
    )
    return conn


async def add_knowledge(
    subject: str,
    predicate: str,
    object: str,
    source: str = "",
    confidence: float = 1.0,
) -> str:
    """Add a knowledge triple (subject-predicate-object) to the local knowledge graph."""
    now = datetime.utcnow().isoformat()
    db = _get_db()
    try:
        # Avoid exact duplicates
        existing = db.execute(
            "SELECT id FROM triples WHERE subject=? AND predicate=? AND object=?",
            (subject, predicate, object),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE triples SET source=?, confidence=?, created_at=? WHERE id=?",
                (source, confidence, now, existing[0]),
            )
            db.commit()
            return json.dumps({"updated": existing[0], "subject": subject, "predicate": predicate})

        cursor = db.execute(
            "INSERT INTO triples (subject, predicate, object, source, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (subject, predicate, object, source, confidence, now),
        )
        db.commit()
        return json.dumps({
            "id": cursor.lastrowid,
            "subject": subject,
            "predicate": predicate,
            "object": object,
        })
    finally:
        db.close()


async def query_knowledge(query: str, limit: int = 10) -> str:
    """Query the local knowledge graph. Searches across subjects, predicates, and objects."""
    db = _get_db()
    try:
        rows = db.execute(
            "SELECT id, subject, predicate, object, source, confidence, created_at "
            "FROM triples "
            "WHERE subject LIKE ? OR predicate LIKE ? OR object LIKE ? "
            "ORDER BY confidence DESC, created_at DESC LIMIT ?",
            (f"%{query}%", f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return json.dumps({
            "count": len(rows),
            "triples": [
                {
                    "id": r[0], "subject": r[1], "predicate": r[2],
                    "object": r[3], "source": r[4], "confidence": r[5],
                    "created_at": r[6],
                }
                for r in rows
            ],
        })
    finally:
        db.close()


async def get_entity(entity: str, limit: int = 20) -> str:
    """Get all knowledge triples where entity appears as subject or object."""
    db = _get_db()
    try:
        rows = db.execute(
            "SELECT id, subject, predicate, object, source, confidence "
            "FROM triples WHERE subject = ? OR object = ? "
            "ORDER BY confidence DESC LIMIT ?",
            (entity, entity, limit),
        ).fetchall()
        return json.dumps({
            "entity": entity,
            "count": len(rows),
            "triples": [
                {
                    "id": r[0], "subject": r[1], "predicate": r[2],
                    "object": r[3], "source": r[4], "confidence": r[5],
                }
                for r in rows
            ],
        })
    finally:
        db.close()


async def delete_knowledge(triple_id: int) -> str:
    """Delete a knowledge triple by ID."""
    db = _get_db()
    try:
        cursor = db.execute("DELETE FROM triples WHERE id = ?", (triple_id,))
        db.commit()
        if cursor.rowcount > 0:
            return json.dumps({"deleted": triple_id})
        return json.dumps({"error": f"Triple {triple_id} not found"})
    finally:
        db.close()
