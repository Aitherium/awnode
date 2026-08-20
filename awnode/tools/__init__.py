"""
AwNode Standalone Tools
============================

Lightweight tool modules that work offline (shell_local scope) and get
enhanced when Genesis or cloud backends are available.

Each module exposes async functions that return JSON strings.
Local data is stored under AITHER_HOME (default: ~/.aither).
"""

from pathlib import Path
import os

AITHER_HOME = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
GENESIS_URL = os.environ.get("AITHER_URL", "http://localhost:8001")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
