"""Run the real Explorer only with an explicitly provisioned API key."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def configured_key() -> str:
    value = os.getenv("SEMANTICA_API_KEY", "").strip()
    filename = os.getenv("SEMANTICA_API_KEY_FILE")
    if not value and filename:
        with Path(filename).open(encoding="utf-8") as stream:
            value = stream.read(8193).strip()
    if not 32 <= len(value) <= 8192 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("a provisioned API key of at least 32 characters is required")
    return value


class AuthenticatedExplorer:
    """Bridge browser HTTP authentication to the upstream X-API-Key contract.

    The bundled upstream UI has no key entry screen or fetch-header injection.
    HTTP Basic authentication works for its same-origin fetches and WebSocket.
    """

    def __init__(self, app: Any, key: str) -> None:
        self.app = app
        self.key = key

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"} or (
            scope["type"] == "http" and scope.get("path") == "/api/health"
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        candidate = headers.get(b"x-api-key", b"")
        authorization = headers.get(b"authorization", b"")
        if authorization.startswith(b"Bearer "):
            candidate = authorization[7:]
        elif authorization.startswith(b"Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True)
                username, separator, password = decoded.partition(b":")
                candidate = password if separator and username == b"admin" else b""
            except (ValueError, binascii.Error):
                candidate = b""
        if not candidate or not hmac.compare_digest(candidate, self.key.encode("ascii")):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
            else:
                body = b'{"error":"authentication_required"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                            (
                                b"www-authenticate",
                                b'Basic realm="Semantica Explorer", charset="UTF-8"',
                            ),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] == "http" and scope.get("method") not in {"GET", "HEAD", "OPTIONS"}:
            origin = headers.get(b"origin")
            host = headers.get(b"host", b"").decode("latin-1")
            if origin and urlsplit(origin.decode("latin-1")).netloc != host:
                body = b'{"error":"origin_not_allowed"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        authenticated = dict(scope)
        authenticated["headers"] = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name not in {b"x-api-key", b"authorization"}
        ] + [(b"x-api-key", self.key.encode("ascii"))]
        await self.app(authenticated, receive, send)


def main() -> None:
    try:
        os.environ["SEMANTICA_API_KEY"] = configured_key()
    except (OSError, ValueError):
        sys.exit(
            "Semantica authentication is not provisioned: set SEMANTICA_API_KEY_FILE "
            "to a protected readable file, or provide SEMANTICA_API_KEY."
        )
    # The stack never opts into the upstream development-only anonymous mode.
    os.environ["SEMANTICA_ALLOW_ANONYMOUS"] = "false"
    import uvicorn
    from semantica.explorer.app import app

    uvicorn.run(
        AuthenticatedExplorer(app, os.environ["SEMANTICA_API_KEY"]),
        host="0.0.0.0",
        port=8000,
        access_log=False,
    )


if __name__ == "__main__":
    main()
