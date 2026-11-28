"""
Lightweight HTTP helpers for standalone awnode.

Provides consistent error handling and response formatting for tool modules
that call Genesis, cloud gateway, or other HTTP services.

Unlike the platform version, this has:
- No TLS/CA bundle complexity
- No lib.core imports
- Plain httpx with sensible defaults

Usage::

    from ._http import genesis_get, genesis_post, cloud_post, fmt_error

    result = await genesis_get("/health")
    result = await cloud_post("/api/chat", {"message": "hello"})
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("awnode.http")

# ---------------------------------------------------------------------------
# Configuration — resolved from environment
# ---------------------------------------------------------------------------

GENESIS_URL = os.environ.get(
    "AITHER_GENESIS_URL",
    os.environ.get("AITHER_URL", "http://localhost:8001"),
)
CLOUD_GATEWAY_URL = os.environ.get(
    "AITHER_CLOUD_URL", "https://gateway.aitherium.com"
)
API_KEY = os.environ.get("AITHER_API_KEY", "")

DEFAULT_TIMEOUT = 30.0
LONG_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

def fmt_error(e: Exception, context: str = "") -> Dict[str, Any]:
    """Format an exception into a structured error dict.

    Args:
        e: The exception to format.
        context: Optional context string describing the operation.

    Returns:
        Dict with an ``error`` key containing a human-readable message.
    """
    msg = str(e)
    if not msg:
        msg = type(e).__name__
    if context:
        msg = f"{context}: {msg}"
    return {"error": msg}


def fmt_http_error(resp: httpx.Response, context: str = "") -> Dict[str, Any]:
    """Format an HTTP error response into a structured error dict.

    Args:
        resp: The httpx response object.
        context: Optional context string describing the operation.

    Returns:
        Dict with an ``error`` key containing status code and response body.
    """
    body = resp.text[:500] if resp.text else "(empty)"
    prefix = f"{context}: " if context else ""
    return {"error": f"{prefix}HTTP {resp.status_code}: {body}"}


# ---------------------------------------------------------------------------
# Core request helper
# ---------------------------------------------------------------------------

async def _async_request(
    method: str,
    url: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    context: str = "",
) -> Dict[str, Any]:
    """Make an async HTTP request with structured error handling.

    Args:
        method: HTTP method (GET, POST, PUT, etc.).
        url: Full URL to request.
        json_body: Optional JSON body for POST/PUT requests.
        headers: Optional extra headers.
        timeout: Request timeout in seconds.
        context: Human-readable context for error messages.

    Returns:
        Parsed JSON response on success, or an error dict on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            kwargs: Dict[str, Any] = {}
            if json_body is not None:
                kwargs["json"] = json_body
            if headers:
                kwargs["headers"] = headers

            resp = await client.request(method, url, **kwargs)

            if resp.status_code == 200:
                return resp.json()
            return fmt_http_error(resp, context or f"{method} {url}")

    except httpx.ConnectError:
        host = url.split("/")[2] if url.count("/") >= 2 else url
        return {"error": f"Connection refused -- service not running at {host}"}
    except httpx.TimeoutException:
        return {"error": f"Timeout after {timeout}s on {method} {context or url}"}
    except Exception as e:
        return fmt_error(e, context or f"{method} {url}")


# ---------------------------------------------------------------------------
# Genesis helpers
# ---------------------------------------------------------------------------

async def genesis_get(
    endpoint: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """GET from Genesis with structured error handling.

    Args:
        endpoint: API path (e.g. ``/health``).
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response or error dict.
    """
    return await _async_request(
        "GET", f"{GENESIS_URL}{endpoint}",
        timeout=timeout,
        context=f"Genesis GET {endpoint}",
    )


async def genesis_post(
    endpoint: str,
    payload: Dict[str, Any],
    timeout: float = LONG_TIMEOUT,
) -> Dict[str, Any]:
    """POST to Genesis with structured error handling.

    Args:
        endpoint: API path (e.g. ``/chat``).
        payload: JSON body to send.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response or error dict.
    """
    return await _async_request(
        "POST", f"{GENESIS_URL}{endpoint}",
        json_body=payload,
        timeout=timeout,
        context=f"Genesis POST {endpoint}",
    )


# ---------------------------------------------------------------------------
# Generic service helpers
# ---------------------------------------------------------------------------

async def service_get(
    base_url: str,
    endpoint: str,
    timeout: float = DEFAULT_TIMEOUT,
    context: str = "",
) -> Dict[str, Any]:
    """GET from any service with structured error handling.

    Args:
        base_url: Service base URL (e.g. ``http://localhost:8080``).
        endpoint: API path.
        timeout: Request timeout in seconds.
        context: Human-readable context for error messages.

    Returns:
        Parsed JSON response or error dict.
    """
    return await _async_request(
        "GET", f"{base_url}{endpoint}",
        timeout=timeout,
        context=context or f"GET {endpoint}",
    )


async def service_post(
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
    context: str = "",
) -> Dict[str, Any]:
    """POST to any service with structured error handling.

    Args:
        base_url: Service base URL.
        endpoint: API path.
        payload: JSON body to send.
        timeout: Request timeout in seconds.
        context: Human-readable context for error messages.

    Returns:
        Parsed JSON response or error dict.
    """
    return await _async_request(
        "POST", f"{base_url}{endpoint}",
        json_body=payload,
        timeout=timeout,
        context=context or f"POST {endpoint}",
    )


# ---------------------------------------------------------------------------
# Cloud gateway helper
# ---------------------------------------------------------------------------

async def cloud_post(
    endpoint: str,
    payload: Dict[str, Any],
    timeout: float = LONG_TIMEOUT,
) -> Dict[str, Any]:
    """POST to the Elysium cloud gateway with API key authentication.

    Args:
        endpoint: API path (e.g. ``/api/chat``).
        payload: JSON body to send.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response or error dict.

    Raises:
        No exceptions -- errors are returned as dicts.
    """
    if not API_KEY:
        return {
            "error": "No API key configured. Set AITHER_API_KEY or run: awnode connect"
        }

    headers = {"Authorization": f"Bearer {API_KEY}"}
    return await _async_request(
        "POST", f"{CLOUD_GATEWAY_URL}{endpoint}",
        json_body=payload,
        headers=headers,
        timeout=timeout,
        context=f"Cloud POST {endpoint}",
    )


async def cloud_get(
    endpoint: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """GET from the Elysium cloud gateway with API key authentication.

    Args:
        endpoint: API path.
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response or error dict.
    """
    if not API_KEY:
        return {
            "error": "No API key configured. Set AITHER_API_KEY or run: awnode connect"
        }

    headers = {"Authorization": f"Bearer {API_KEY}"}
    return await _async_request(
        "GET", f"{CLOUD_GATEWAY_URL}{endpoint}",
        headers=headers,
        timeout=timeout,
        context=f"Cloud GET {endpoint}",
    )
