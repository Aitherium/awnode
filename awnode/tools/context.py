"""
Context Tools
==============

In-memory session context for the current process.
When Genesis is available, proxies to the full ContextPipeline.
"""

import json
from datetime import datetime
from typing import Dict

# Session-scoped context store (resets on process restart)
_context: Dict[str, str] = {}
_topic: str = ""
_session_start: str = datetime.utcnow().isoformat()


async def get_context(topic: str = "") -> str:
    """Get current conversation context and active topic."""
    result = {
        "topic": topic or _topic,
        "session_start": _session_start,
        "variables": dict(_context),
        "count": len(_context),
    }
    return json.dumps(result)


async def set_context(key: str, value: str) -> str:
    """Set a context variable for the current session."""
    global _topic
    _context[key] = value
    if key == "topic":
        _topic = value
    return json.dumps({"set": key, "value": value, "total_keys": len(_context)})


async def clear_context() -> str:
    """Clear all session context variables."""
    global _topic
    count = len(_context)
    _context.clear()
    _topic = ""
    return json.dumps({"cleared": count})


async def get_context_key(key: str) -> str:
    """Get a specific context variable by key."""
    if key in _context:
        return json.dumps({"key": key, "value": _context[key]})
    return json.dumps({"error": f"Context key '{key}' not found"})


async def delete_context_key(key: str) -> str:
    """Delete a specific context variable."""
    global _topic
    if key in _context:
        del _context[key]
        if key == "topic":
            _topic = ""
        return json.dumps({"deleted": key})
    return json.dumps({"error": f"Context key '{key}' not found"})
