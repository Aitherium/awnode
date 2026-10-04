"""
HTTP Client Tool
=================

General-purpose HTTP client for making web requests.
Uses httpx for async HTTP with timeout protection.
"""

import json


async def http_request(
    url: str,
    method: str = "GET",
    body: str = "",
    headers: str = "{}",
    timeout: float = 30.0,
) -> str:
    """Make an HTTP request and return the response."""
    import httpx

    try:
        parsed_headers = json.loads(headers) if headers else {}
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid headers JSON"})

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            kwargs: dict = {"headers": parsed_headers}

            if body and method.upper() in ("POST", "PUT", "PATCH"):
                # Try to send as JSON if it parses, otherwise as text
                try:
                    kwargs["json"] = json.loads(body)
                except json.JSONDecodeError:
                    kwargs["content"] = body
                    if "Content-Type" not in parsed_headers:
                        kwargs["headers"]["Content-Type"] = "text/plain"

            response = await client.request(method.upper(), url, **kwargs)

            # Try to parse response as JSON
            try:
                response_body = response.json()
            except Exception:
                response_body = response.text

            return json.dumps({
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": response_body,
                "url": str(response.url),
                "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
            })

    except httpx.TimeoutException:
        return json.dumps({"error": f"Request timed out after {timeout}s", "url": url})
    except httpx.ConnectError:
        return json.dumps({"error": f"Connection failed: {url}", "url": url})
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})
