"""
awnode Browser Proxy Plane
==============================

Allowlisted reverse proxy that lets a browser surface (the Awconnect
extension, portal.aitherium.com, or a localhost web app) reach host-machine
AitherOS services over plain HTTP on the node's loopback port.

Why this exists: Chrome extension service workers cannot fetch self-signed
HTTPS endpoints, and every AitherOS service binds HTTPS with internal-CA
certs. Historically the extension worked around this via the Veil dev
server's /api/bridge proxy (port 3000) — which couples the browser to a
whole Next.js deployment. awnode is the durable replacement: a tiny
persistent host service that terminates plain HTTP on loopback and proxies
to the internal-CA HTTPS services with REAL certificate verification.

Security model (fail-closed):
- Only services in the allowlist are reachable; unknown names -> 404.
- Callers must be loopback peers, OR present the shared proxy token
  (AITHERNODE_PROXY_TOKEN) — a non-loopback caller without the token gets
  403 on every path. This keeps a 0.0.0.0-bound node from becoming an open
  LAN pivot into Genesis.
- Upstream TLS is verified against the AitherNet internal CA when a bundle
  is configured (AITHER_CA_BUNDLE / ~/.aither/ca.pem); NEVER verify=False.
  A missing CA for an HTTPS upstream surfaces as an explicit 502 with a
  remediation hint instead of silently disabling verification.
"""

import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger("awnode.proxy")

router = APIRouter(prefix="/proxy", tags=["proxy"])

# ── Service allowlist ───────────────────────────────────────────────
#
# name -> upstream base URL. https upstreams are verified against the
# internal CA (see _verify()). Extend/override with AITHERNODE_PROXY_MAP,
# a JSON object of {name: base_url}; entries here are DEFAULTS.

_DEFAULT_SERVICES: Dict[str, str] = {
    # AitherSearch — federated web + platform search (cognition layer)
    "search": os.environ.get("AITHER_SEARCH_URL", "https://127.0.0.1:8114"),
    # AitherBrowser — Playwright automation / capture / crawl (perception layer)
    "browser": os.environ.get("AITHER_BROWSER_URL", "https://127.0.0.1:8132"),
    # Genesis via its nginx LB — plain HTTP on the host (documented exception)
    "genesis": os.environ.get("AITHER_URL", "http://127.0.0.1:8001"),
    # Bonsai-27B llama.cpp server (PrismML fork) — OpenAI-compatible /v1
    "bonsai": os.environ.get("AITHER_BONSAI_URL", "http://127.0.0.1:8092"),
    # Ollama — generic local model host
    "ollama": os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
    # AitherMarketplace — unified catalog (agent/skill/tool packs, apps,
    # tools, plugins, extensions) with the fail-closed entitlement gate.
    "marketplace": os.environ.get("AITHER_MARKETPLACE_URL", "https://127.0.0.1:8260"),
}


def _load_service_map() -> Dict[str, str]:
    services = dict(_DEFAULT_SERVICES)
    raw = os.environ.get("AITHERNODE_PROXY_MAP", "")
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                for name, url in extra.items():
                    if isinstance(name, str) and isinstance(url, str) and url:
                        services[name.strip().lower()] = url.rstrip("/")
        except (ValueError, TypeError) as e:
            logger.warning("AITHERNODE_PROXY_MAP is not valid JSON — ignored (%s)", e)

    def _norm(url: str) -> str:
        url = url.strip().rstrip("/")
        # OLLAMA_HOST is commonly "0.0.0.0:11434" (no scheme) — normalize so
        # httpx gets an absolute URL, and rewrite wildcard binds to loopback.
        if url and "://" not in url:
            url = f"http://{url}"
        return url.replace("://0.0.0.0", "://127.0.0.1")

    return {k: _norm(v) for k, v in services.items()}


SERVICES: Dict[str, str] = _load_service_map()

PROXY_TOKEN = os.environ.get("AITHERNODE_PROXY_TOKEN", "")

# Hop-by-hop headers that must not be forwarded either direction (RFC 7230 §6.1)
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _verify():
    """TLS verification material for internal-CA HTTPS upstreams.

    Returns a CA bundle path when one is configured, else True (standard
    system trust). Never returns False — an unverifiable internal upstream
    should FAIL LOUDLY (502 + hint), not silently skip verification.
    """
    for candidate in (
        os.environ.get("AITHER_CA_BUNDLE", ""),
        str(Path.home() / ".aither" / "ca.pem"),
        "/etc/aither/ca.pem",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return True


def _client_is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authorize(request: Request) -> None:
    """Loopback callers pass; remote callers need the proxy token. Fail closed."""
    if _client_is_loopback(request):
        return
    if PROXY_TOKEN:
        presented = request.headers.get("x-aithernode-proxy-token", "")
        if presented and presented == PROXY_TOKEN:
            return
    raise HTTPException(403, "proxy access denied (non-loopback caller without proxy token)")


def _upstream_for(service: str) -> str:
    base = SERVICES.get(service.lower())
    if not base:
        raise HTTPException(
            404, f"unknown proxy service '{service}' (allowed: {', '.join(sorted(SERVICES))})"
        )
    return base


def _request_headers(request: Request) -> Dict[str, str]:
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    # Never leak the node's proxy token upstream
    headers.pop("x-aithernode-proxy-token", None)
    # Force identity encoding to the upstream. If we forwarded the caller's
    # `Accept-Encoding: gzip` (browsers always send it) — OR let httpx pick its
    # own default `gzip, deflate, br, zstd` — the upstream may reply brotli/zstd,
    # which httpx can't decode without the optional codecs and raises an
    # empty-message DecodingError → every proxied browser call 502s. Asking for
    # identity means the upstream never compresses and there's nothing to decode
    # or mislabel. (This handler re-frames the body, so we own the encoding.)
    for k in list(headers):
        if k.lower() == "accept-encoding":
            del headers[k]
    headers["accept-encoding"] = "identity"
    return headers


# Content-Encoding / Content-Length are stripped from the response because
# httpx already decoded the body and we re-frame it — forwarding the upstream's
# encoding/length headers would misdescribe the bytes we send.
_RESP_STRIP = _HOP_BY_HOP | {"content-encoding"}


def _response_headers(upstream: httpx.Response) -> Dict[str, str]:
    return {
        k: v for k, v in upstream.headers.items() if k.lower() not in _RESP_STRIP
    }


_STREAM_HINTS = ("text/event-stream", "application/x-ndjson")


@router.get("/services")
async def list_services(request: Request):
    """Enumerate the proxy allowlist and live upstream health."""
    _authorize(request)
    out = {}
    verify = _verify()
    async with httpx.AsyncClient(timeout=3.0, verify=verify) as c:
        for name, base in SERVICES.items():
            try:
                r = await c.get(f"{base}/health")
                out[name] = {"url": base, "healthy": r.status_code == 200}
            except httpx.HTTPError:
                out[name] = {"url": base, "healthy": False}
    return {"services": out, "ca_verified": verify is not True}


@router.api_route(
    "/{service}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(service: str, path: str, request: Request):
    """Forward a request to an allowlisted host service, streaming-safe."""
    _authorize(request)
    base = _upstream_for(service)
    url = f"{base}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    body = await request.body()
    headers = _request_headers(request)

    client = httpx.AsyncClient(
        verify=_verify(),
        timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=5.0),
    )
    try:
        upstream_req = client.build_request(
            request.method, url, headers=headers, content=body or None
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.ConnectError as e:
        await client.aclose()
        detail = f"upstream '{service}' unreachable at {base}: {e}"
        if base.startswith("https://") and "certificate" in str(e).lower():
            detail += (
                " — TLS verification failed; point AITHER_CA_BUNDLE at the "
                "AitherNet internal CA bundle (or copy it to ~/.aither/ca.pem)"
            )
        raise HTTPException(502, detail) from e
    except httpx.HTTPError as e:
        await client.aclose()
        raise HTTPException(502, f"upstream '{service}' error: {e}") from e

    content_type = upstream.headers.get("content-type", "")
    if any(h in content_type for h in _STREAM_HINTS):
        # SSE / NDJSON — stream chunks through without buffering
        async def _relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _relay(),
            status_code=upstream.status_code,
            headers=_response_headers(upstream),
            media_type=content_type or "text/event-stream",
        )

    try:
        payload = await upstream.aread()
    except httpx.HTTPError as e:
        await upstream.aclose()
        await client.aclose()
        # Surface the exception TYPE — a bare str(e) is empty for DecodingError
        # and hid the gzip/brotli issue that made this look like a silent 502.
        raise HTTPException(
            502, f"upstream '{service}' read error: {type(e).__name__}: {e}"
        ) from e
    finally:
        # aclose() is idempotent; safe even after the except above closed them.
        await upstream.aclose()
        await client.aclose()
    return Response(
        content=payload,
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=content_type or None,
    )


def refresh_services() -> Dict[str, str]:
    """Re-read env-derived service map (used by tests and config reload)."""
    global SERVICES
    SERVICES = _load_service_map()
    return SERVICES


def bonsai_url() -> Optional[str]:
    return SERVICES.get("bonsai")
