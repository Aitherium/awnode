"""
awnode MCP Server — Standalone Edition
============================================

Model Context Protocol server for the standalone ``awnode`` pip package.
Works WITHOUT the full AitherOS platform by auto-discovering tool modules
from ``awnode/tools/`` and filtering them by runtime scope.

Runtime modes (detected automatically on startup):
    LOCAL       Genesis is available on the network
    CLOUD       No Genesis, but AITHER_API_KEY is set (Elysium gateway)
    STANDALONE  Fully offline — only shell_local tools are active

Transports:
    stdio   For Claude Code / Cursor IDE integrations
    sse     HTTP SSE for web clients (default port 8090)

Usage::

    # stdio (IDE integration)
    awnode mcp

    # SSE (web clients)
    awnode mcp --transport sse --port 8090

    # Programmatic
    from awnode.mcp_server import run_stdio, run_sse
    import asyncio
    asyncio.run(run_stdio())
"""

from __future__ import annotations

import asyncio
import hmac
import importlib
import inspect
import json
import logging
import os
import pkgutil
import sys
from enum import Enum
from typing import Any, Callable, Dict, List, get_type_hints

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger("awnode.mcp")

# ---------------------------------------------------------------------------
# Runtime mode
# ---------------------------------------------------------------------------

class RuntimeMode(str, Enum):
    LOCAL = "local"           # Genesis reachable
    CLOUD = "cloud"           # Elysium API key, no local Genesis
    STANDALONE = "standalone" # Fully offline


GENESIS_URL = os.environ.get(
    "AITHER_GENESIS_URL",
    os.environ.get("AITHER_URL", "http://localhost:8001"),
)
CLOUD_GATEWAY_URL = os.environ.get(
    "AITHER_CLOUD_URL", "https://gateway.aitherium.com"
)
API_KEY = os.environ.get("AITHER_API_KEY", "")

# Inbound auth for the network-exposed SSE transport. The SSE server exposes
# tool EXECUTION (including shell_local tools) over HTTP, so it must never be
# reachable on a non-loopback interface without a shared secret. Set via env.
MCP_AUTH_KEY_ENV = "AITHER_MCP_KEY"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


def _bearer_ok(auth_header: str, key: str) -> bool:
    """Constant-time check that an Authorization header carries the right bearer.

    Fail-closed: an empty key, missing/short header, wrong scheme, or empty
    token all return False. Uses hmac.compare_digest to avoid leaking the key
    position-by-position to a timing attacker over the network.
    """
    if not key:
        return False
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    return bool(token) and hmac.compare_digest(token, key)


async def _probe(url: str) -> bool:
    """Quick health check against a URL (2-second timeout)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{url.rstrip('/')}/health")
            return r.status_code == 200
    except ImportError:
        return False


async def detect_mode() -> RuntimeMode:
    """Probe backends and determine the runtime mode."""
    if await _probe(GENESIS_URL):
        return RuntimeMode.LOCAL
    if API_KEY and await _probe(CLOUD_GATEWAY_URL):
        return RuntimeMode.CLOUD
    return RuntimeMode.STANDALONE


# ---------------------------------------------------------------------------
# Scopes — which tool modules are available in which mode
# ---------------------------------------------------------------------------

from awnode.tools._scopes import SHELL_LOCAL, SHELL_CLOUD, PLATFORM_ONLY  # noqa: E402


def _scope_for_module(module_name: str) -> str:
    """Return 'shell_local', 'shell_cloud', or 'platform_only' for a module."""
    if module_name in SHELL_LOCAL:
        return "shell_local"
    if module_name in SHELL_CLOUD:
        return "shell_cloud"
    if module_name in PLATFORM_ONLY:
        return "platform_only"
    # Unlisted modules default to platform_only
    return "platform_only"


def _mode_allows_scope(mode: RuntimeMode, scope: str) -> bool:
    """Check whether a runtime mode permits a given tool scope."""
    if scope == "shell_local":
        return True  # Always available
    if scope == "shell_cloud":
        return mode in (RuntimeMode.LOCAL, RuntimeMode.CLOUD)
    if scope == "platform_only":
        return mode == RuntimeMode.LOCAL
    return False


# ---------------------------------------------------------------------------
# Tool registry — auto-discover from awnode.tools.*
# ---------------------------------------------------------------------------

# Mapping: tool_name -> {func, module, scope, description, schema}
_TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def _python_type_to_json(annotation: Any) -> Dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}
    origin = getattr(annotation, "__origin__", None)

    # Handle Optional[X] (Union[X, None])
    if origin is type(None):
        return {"type": "string"}
    args = getattr(annotation, "__args__", None)
    if origin is not None and args is not None:
        # Optional = Union[X, None]
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            schema = _python_type_to_json(non_none[0])
            return schema
        return {"type": "string"}

    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is list or annotation is List:
        return {"type": "array", "items": {"type": "string"}}
    if annotation is dict or annotation is Dict:
        return {"type": "object"}
    return {"type": "string"}


def _build_schema_for_func(func: Callable) -> Dict[str, Any]:
    """Build a JSON Schema 'inputSchema' from function signature + type hints."""
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    properties: Dict[str, Any] = {}
    required: List[str] = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        annotation = hints.get(name, param.annotation)
        prop = _python_type_to_json(annotation)

        # Extract description from docstring Args section (best-effort)
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            prop["default"] = param.default

        properties[name] = prop

    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _extract_description(func: Callable) -> str:
    """Extract tool description from function docstring (first non-empty line)."""
    doc = inspect.getdoc(func) or ""
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return func.__name__.replace("_", " ").title()


def _discover_tools() -> Dict[str, Dict[str, Any]]:
    """Auto-discover tool functions from awnode.tools.* modules.

    A function is registered as a tool if:
    - It is defined in a tools submodule (not prefixed with ``_``)
    - It is listed in the module's ``__all__``, or
    - It is a public async/sync function (no leading underscore)
    - It is not a class or non-callable
    """
    registry: Dict[str, Dict[str, Any]] = {}
    tools_package = importlib.import_module("awnode.tools")
    tools_path = getattr(tools_package, "__path__", [])

    for importer, modname, ispkg in pkgutil.iter_modules(tools_path):
        if modname.startswith("_"):
            continue  # Skip private modules (_http, _scopes, etc.)

        full_name = f"awnode.tools.{modname}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as exc:
            logger.warning("Failed to import tool module %s: %s", full_name, exc)
            continue

        # Determine which functions to export
        exports = getattr(mod, "__all__", None)
        if exports is None:
            exports = [
                name for name, obj in inspect.getmembers(mod, callable)
                if not name.startswith("_") and inspect.getmodule(obj) is mod
            ]

        scope = _scope_for_module(modname)

        for func_name in exports:
            func = getattr(mod, func_name, None)
            if func is None or not callable(func):
                continue

            description = _extract_description(func)
            schema = _build_schema_for_func(func)

            registry[func_name] = {
                "func": func,
                "module": modname,
                "scope": scope,
                "description": description,
                "schema": schema,
            }

    return registry


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("awnode")
_current_mode: RuntimeMode = RuntimeMode.STANDALONE


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return tools available for the current runtime mode."""
    tools: list[Tool] = []
    for name, info in _TOOL_REGISTRY.items():
        if not _mode_allows_scope(_current_mode, info["scope"]):
            continue
        tools.append(Tool(
            name=name,
            description=info["description"],
            inputSchema=info["schema"],
        ))
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Invoke a registered tool by name."""
    info = _TOOL_REGISTRY.get(name)
    if info is None:
        return [TextContent(
            type="text",
            text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2),
        )]

    # Scope check
    if not _mode_allows_scope(_current_mode, info["scope"]):
        scope = info["scope"]
        if scope == "platform_only":
            msg = (
                "This tool requires the full AitherOS platform. "
                "Start AitherOS or connect to Elysium cloud."
            )
        elif scope == "shell_cloud":
            msg = (
                "This tool requires an API key. "
                "Set AITHER_API_KEY or connect to Elysium: awnode connect"
            )
        else:
            msg = f"Tool '{name}' is not available in {_current_mode.value} mode."
        return [TextContent(
            type="text",
            text=json.dumps({"error": msg, "scope": scope}, indent=2),
        )]

    # License/subscription check
    try:
        from awnode.license import get_license_manager
        denial = get_license_manager().check_tool(info["module"])
        if denial:
            return [TextContent(
                type="text",
                text=json.dumps({"error": denial, "scope": "subscription"}, indent=2),
            )]
    except ImportError:
        pass  # License check failure should never block tools

    func = info["func"]

    try:
        # Filter arguments to only those the function accepts
        sig = inspect.signature(func)
        valid_params = set(sig.parameters.keys()) - {"self", "cls"}
        filtered_args = {k: v for k, v in arguments.items() if k in valid_params}

        if asyncio.iscoroutinefunction(func):
            result = await func(**filtered_args)
        else:
            result = func(**filtered_args)

        # Ensure result is a string
        if not isinstance(result, str):
            result = json.dumps(result, indent=2, default=str)

        return [TextContent(type="text", text=result)]
    except Exception as exc:
        error_msg = str(exc) or type(exc).__name__
        return [TextContent(
            type="text",
            text=json.dumps({"error": error_msg, "tool": name}, indent=2),
        )]


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

async def _initialize() -> None:
    """Probe backends and populate the tool registry."""
    global _current_mode, _TOOL_REGISTRY
    _current_mode = await detect_mode()
    _TOOL_REGISTRY = _discover_tools()

    tool_count = sum(
        1 for info in _TOOL_REGISTRY.values()
        if _mode_allows_scope(_current_mode, info["scope"])
    )
    total = len(_TOOL_REGISTRY)
    logger.info(
        "awnode MCP ready — mode=%s, tools=%d/%d available",
        _current_mode.value, tool_count, total,
    )


# ---------------------------------------------------------------------------
# Transport entry points
# ---------------------------------------------------------------------------

async def run_stdio() -> None:
    """Run the MCP server over stdio (for Claude Code / Cursor)."""
    await _initialize()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def run_sse(host: str = "0.0.0.0", port: int = 8090) -> None:
    """Run the MCP server over HTTP SSE (for web clients).

    Args:
        host: Bind address (default: 0.0.0.0)
        port: Listen port (default: 8090)

    Security:
        The SSE transport exposes tool execution over HTTP. Binding a
        non-loopback interface REQUIRES a bearer secret in ``AITHER_MCP_KEY``;
        without it we refuse to start rather than expose an unauthenticated
        tool-execution surface. When the key is set, every route except
        ``/health`` requires ``Authorization: Bearer <key>``.
    """
    # ── Fail-closed auth guard (runs before any network/tool init) ──────
    mcp_key = os.environ.get(MCP_AUTH_KEY_ENV, "")
    is_loopback = host in _LOOPBACK_HOSTS
    if not mcp_key and not is_loopback:
        logger.error(
            "Refusing to expose MCP tool execution on %s without auth. "
            "Set %s to a shared secret, or bind --host 127.0.0.1 for "
            "local-only access.",
            host, MCP_AUTH_KEY_ENV,
        )
        raise SystemExit(2)

    await _initialize()

    try:
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse
        from starlette.routing import Route, Mount
        import uvicorn
    except ImportError as exc:
        logger.error(
            "SSE transport requires additional dependencies: %s. "
            "Install with: pip install 'awnode[sse]'",
            exc,
        )
        raise SystemExit(1)

    class _BearerAuthMiddleware(BaseHTTPMiddleware):
        """Bearer-token gate on every route except /health."""

        def __init__(self, app, key: str):
            super().__init__(app)
            self._key = key

        async def dispatch(self, request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            if not _bearer_ok(request.headers.get("authorization", ""), self._key):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )

    async def handle_health(request):
        from starlette.responses import JSONResponse
        return JSONResponse({
            "status": "healthy",
            "service": "awnode-mcp",
            "mode": _current_mode.value,
            "tools": sum(
                1 for info in _TOOL_REGISTRY.values()
                if _mode_allows_scope(_current_mode, info["scope"])
            ),
        })

    middleware = [Middleware(_BearerAuthMiddleware, key=mcp_key)] if mcp_key else []
    app = Starlette(
        debug=False,
        middleware=middleware,
        routes=[
            Route("/health", handle_health),
            Mount("/sse", app=sse.get_starlette_app()),
        ],
    )

    if mcp_key:
        logger.info("awnode MCP SSE auth: ENABLED (bearer token required)")
    else:
        logger.warning(
            "awnode MCP SSE auth: DISABLED — loopback-only (no %s set)",
            MCP_AUTH_KEY_ENV,
        )
    logger.info("awnode MCP SSE server starting on %s:%d", host, port)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    srv = uvicorn.Server(config)
    await srv.serve()


def main() -> None:
    """CLI entry point for ``awnode mcp``."""
    import argparse

    parser = argparse.ArgumentParser(description="awnode MCP Server")
    parser.add_argument(
        "--transport", choices=["stdio", "sse"], default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind host for SSE transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=8090,
        help="Port for SSE transport (default: 8090)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    if args.transport == "sse":
        asyncio.run(run_sse(host=args.host, port=args.port))
    else:
        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
