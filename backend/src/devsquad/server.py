"""Loopback FastAPI app.

Only ``GET /health`` is served. ``serve`` binds ``127.0.0.1`` and has no
host argument, so a caller cannot accidentally publish the process.

Binding to loopback is not enough on its own. A web page on another origin
can still try to call a local port, and DNS rebinding can point a public
name at 127.0.0.1. Every request therefore also has to:

* send ``X-DevSquad-Token`` equal to the ``DEVSQUAD_TOKEN`` environment
  variable (compared with ``hmac.compare_digest``)
* use a Host of ``127.0.0.1`` or ``localhost``, with an optional port
* omit ``Origin``, or send an ``http``/``https`` origin on those same hosts
  with no userinfo and no path

An empty token fails closed. The token is never written into a response or
a log line by this module. Forwarded headers such as ``X-Forwarded-Host``
are ignored; this process is not meant to sit behind a proxy.

``/docs`` and the OpenAPI schema are disabled so the only route is the one
the tests cover.
"""

from __future__ import annotations

import hmac
import re
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from devsquad.config import server_token

BIND_HOST = "127.0.0.1"
TOKEN_HEADER = "x-devsquad-token"
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(:[0-9]{1,5})?$")

app = FastAPI(title="DevSquad", docs_url=None, redoc_url=None, openapi_url=None)


def _host_allowed(value: str) -> bool:
    match = _HOST_RE.fullmatch(value.strip().lower())
    if match is None:
        return False
    port = match.group(2)
    if port is None:
        return True
    number = int(port[1:])
    return 1 <= number <= 65535


def _origin_allowed(value: str) -> bool:
    parsed = urlparse(value.strip())
    # ``http://evil@127.0.0.1`` has hostname 127.0.0.1. Reject userinfo
    # explicitly so that trick does not count as loopback.
    if parsed.username or parsed.password:
        return False
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _ALLOWED_HOSTS:
        return False
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        return False
    return True


def _token_ok(presented: str, expected: str) -> bool:
    if not expected or not presented:
        return False
    left = presented.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def authorize(request: Request) -> JSONResponse | None:
    """Return an error response, or None when the request may proceed.

    Host and Origin are checked before the token so a non-local caller is
    rejected even if it knows the token. A local caller with a missing or
    wrong token gets 401. Bodies are generic and do not echo the token.
    """
    if not _host_allowed(request.headers.get("host", "")):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None and not _origin_allowed(origin):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    if not _token_ok(request.headers.get(TOKEN_HEADER, ""), server_token()):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return None


@app.middleware("http")
async def enforce_local_client(request: Request, call_next):
    rejected = authorize(request)
    if rejected is not None:
        return rejected
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe for the local sidecar. No task data is returned."""
    return {"status": "ok"}


def serve(port: int = 8765) -> None:
    """Bind the app to loopback only.

    There is no ``host`` parameter. ``0.0.0.0`` is not a supported
    configuration in this PR.
    """
    import uvicorn

    if port <= 0 or port > 65535:
        raise ValueError("port out of range")
    uvicorn.run(app, host=BIND_HOST, port=port, log_level="info")
