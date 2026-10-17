"""Extension management tools for awnode."""

import json
from typing import Any

from awnode.extensions import get_extension_manager


async def ext_list() -> str:
    """List all available extensions (creative services, agents, tools).

    Returns a JSON object with all extensions from the built-in catalog
    and any custom-registered extensions, along with their current status.
    """
    mgr = get_extension_manager()
    catalog = mgr.list_catalog()
    return json.dumps({"extensions": catalog, "total": len(catalog)}, indent=2, default=str)


async def ext_install(extension_id: str) -> str:
    """Install an extension (pull Docker image or verify local binary).

    Args:
        extension_id: Extension ID from the catalog (e.g., "canvas", "comfyui", "ollama").
    """
    mgr = get_extension_manager()
    result = await mgr.install(extension_id)
    return json.dumps(result, indent=2, default=str)


async def ext_start(extension_id: str) -> str:
    """Start an installed extension.

    Args:
        extension_id: Extension ID to start.
    """
    mgr = get_extension_manager()
    result = await mgr.start(extension_id)
    return json.dumps(result, indent=2, default=str)


async def ext_stop(extension_id: str) -> str:
    """Stop a running extension.

    Args:
        extension_id: Extension ID to stop.
    """
    mgr = get_extension_manager()
    result = await mgr.stop(extension_id)
    return json.dumps(result, indent=2, default=str)


async def ext_status(extension_id: str = "") -> str:
    """Check status of an extension or all extensions.

    Args:
        extension_id: Extension ID to check. If empty, checks all running extensions.
    """
    mgr = get_extension_manager()
    if extension_id:
        result = await mgr.health_check(extension_id)
    else:
        result = await mgr.health_check_all()
    return json.dumps(result, indent=2, default=str)


async def ext_register(manifest: str) -> str:
    """Register a custom extension from a JSON manifest string.

    Args:
        manifest: JSON string with extension manifest. Must include at minimum
            an "id" field. See built-in catalog for full schema.
    """
    mgr = get_extension_manager()
    try:
        data = json.loads(manifest)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON manifest"})
    result = mgr.register_custom(data)
    return json.dumps(result, indent=2, default=str)
