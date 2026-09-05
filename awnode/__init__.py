"""
awnode — Lightweight Local Gateway for AitherOS
====================================================

The bridge between local apps (Shell, Desktop, Connect) and AitherOS services.

Modes:
1. **Local Genesis** — proxy to localhost:8001 (full AitherOS Docker stack)
2. **Cloud** — proxy to mcp.aitherium.com (SaaS inference + MCP tools)
3. **Standalone** — local Ollama + basic tools (no Genesis needed)
4. **Hybrid** — local tools + cloud LLM (best of both)

Install:
    pip install awnode
    aithernode start               # Start on port 8090
    aithernode start --cloud       # Connect to Elysium
    aithernode status              # Check what's connected
"""

__version__ = "0.3.0"
