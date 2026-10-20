"""Authenticate every public Hindsight route and supervise the real standalone.

The two upstream listeners are loopback-only. Next's server-side dataplane
client and the API's native tenant extension receive the same provisioned key.
The Control Plane is an administrator interface; tenant users use Octop.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_BODY_BYTES = 32 * 1024 * 1024
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def configured_token() -> str:
    value = os.getenv("HINDSIGHT_API_TOKEN", "").strip()
    filename = os.getenv("HINDSIGHT_API_TOKEN_FILE")
    if not value and filename:
        with Path(filename).open(encoding="utf-8") as stream:
            value = stream.read(8193).strip()
    if not 32 <= len(value) <= 8192 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("a provisioned Hindsight token of at least 32 characters is required")
    return value


def upstream_environment(token: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HINDSIGHT_API_HOST": "127.0.0.1",
            "HINDSIGHT_API_PORT": "8889",
            "HINDSIGHT_API_HEALTH_URL": "http://127.0.0.1:8889/health",
            "HINDSIGHT_CP_HOSTNAME": "127.0.0.1",
            "HINDSIGHT_CP_PORT": "9998",
            "HINDSIGHT_CP_DATAPLANE_API_URL": "http://127.0.0.1:8889",
            "HINDSIGHT_CP_DATAPLANE_API_KEY": token,
            "HINDSIGHT_API_TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
            "HINDSIGHT_API_TENANT_API_KEY": token,
            "HINDSIGHT_API_TENANT_MCP_AUTH_DISABLED": "false",
            "HINDSIGHT_ENABLE_API": "true",
            "HINDSIGHT_ENABLE_CP": "true",
        }
    )
    # The outer Basic challenge is the browser login. Avoid a second cookie login.
    env.pop("HINDSIGHT_CP_ACCESS_KEY", None)
    return env


class AuthenticatedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, upstream_port: int, token: str, health_path: str) -> None:
        self.upstream_port = upstream_port
        self.token = token
        self.health_path = health_path
        super().__init__(("0.0.0.0", port), ProxyHandler)

    def handle_error(self, request: socket.socket, client_address: object) -> None:
        # Do not print request paths, headers, exception payloads or credentials.
        print("Hindsight proxy request failed", file=sys.stderr, flush=True)


class ProxyHandler(BaseHTTPRequestHandler):
    server: AuthenticatedServer
    protocol_version = "HTTP/1.1"
    server_version = "OctopHindsight"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(90)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _authenticated(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        candidate = b""
        if authorization.startswith("Bearer "):
            candidate = authorization[7:].encode("utf-8")
        elif authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True)
                username, separator, password = decoded.partition(b":")
                if separator and username == b"admin":
                    candidate = password
            except (ValueError, binascii.Error):
                pass
        return bool(candidate) and hmac.compare_digest(candidate, self.server.token.encode("ascii"))

    def _json(self, status: int, value: dict[str, object], challenge: bool = False) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if challenge:
            self.send_header(
                "WWW-Authenticate", 'Basic realm="Hindsight administrator", charset="UTF-8"'
            )
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_expect_100(self) -> bool:
        self._json(417, {"error": "expectation_not_supported"})
        return False

    def _body(self) -> bytes:
        transfer = self.headers.get_all("Transfer-Encoding", [])
        lengths = self.headers.get_all("Content-Length", [])
        if transfer:
            if len(transfer) != 1 or transfer[0].lower().strip() != "chunked" or lengths:
                raise ValueError("invalid request framing")
            parts: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(128)
                if not line.endswith(b"\r\n"):
                    raise ValueError("invalid chunk")
                size = int(line.strip().split(b";", 1)[0], 16)
                if size < 0 or total + size > MAX_BODY_BYTES:
                    raise ValueError("body too large")
                if size == 0:
                    # Bounded trailers; never forward them to the upstream.
                    trailer_size = 0
                    while True:
                        trailer = self.rfile.readline(8193)
                        trailer_size += len(trailer)
                        if not trailer or not trailer.endswith(b"\r\n") or trailer_size > 8192:
                            raise ValueError("invalid trailers")
                        if trailer == b"\r\n":
                            return b"".join(parts)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("incomplete body")
                parts.append(chunk)
                total += size
        if len(lengths) > 1:
            raise ValueError("duplicate content length")
        length = int(lengths[0]) if lengths else 0
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError("body too large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("incomplete body")
        return body

    def _health(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=3)
        try:
            connection.request("GET", self.server.health_path)
            response = connection.getresponse()
            healthy = response.status == 200
        except (OSError, http.client.HTTPException):
            healthy = False
        finally:
            connection.close()
        self._json(200 if healthy else 503, {"status": "ok" if healthy else "starting"})

    def _proxy(self) -> None:
        if self.command in {"GET", "HEAD"} and self.path == self.server.health_path:
            self._health()
            return
        if not self._authenticated():
            self._json(401, {"error": "authentication_required"}, challenge=True)
            return
        if not self.path.startswith("/") or self.path.startswith("//"):
            self._json(400, {"error": "invalid_request_target"})
            return
        if self.headers.get("Upgrade"):
            self._json(426, {"error": "websocket_not_supported"})
            return
        if self.command not in {"GET", "HEAD", "OPTIONS"}:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self._json(403, {"error": "origin_not_allowed"})
                return
        try:
            body = self._body()
        except (ValueError, OSError):
            self._json(400, {"error": "invalid_request_body"})
            return
        excluded = HOP_HEADERS | {"authorization", "x-api-key", "content-length", "expect"}
        excluded |= {item.strip().lower() for item in self.headers.get("Connection", "").split(",")}
        headers = {
            name: value for name, value in self.headers.items() if name.lower() not in excluded
        }
        headers["Content-Length"] = str(len(body))
        headers["Connection"] = "close"
        # Do not let client Forwarded headers influence Next's internal requests.
        headers = {
            name: value
            for name, value in headers.items()
            if name.lower() != "forwarded" and not name.lower().startswith("x-forwarded-")
        }
        if self.server.upstream_port == 8889:
            headers["Authorization"] = "Bearer " + self.server.token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=180)
        response_started = False
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            excluded_response = HOP_HEADERS | {"server", "date"}
            excluded_response |= {
                item.strip().lower() for item in response.getheader("Connection", "").split(",")
            }
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in excluded_response:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            response_started = True
            self.close_connection = True
            if self.command != "HEAD":
                while chunk := response.read1(64 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not response_started:
                self._json(502, {"error": "upstream_unavailable"})
        finally:
            connection.close()
            self.close_connection = True

    do_GET = _proxy
    do_HEAD = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_PATCH = _proxy
    do_DELETE = _proxy
    do_OPTIONS = _proxy


def main() -> None:
    try:
        token = configured_token()
    except (OSError, ValueError):
        sys.exit(
            "Hindsight requires a protected readable HINDSIGHT_API_TOKEN_FILE or HINDSIGHT_API_TOKEN."
        )
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    servers: list[AuthenticatedServer] = []
    child: subprocess.Popen[bytes] | None = None
    exit_code = 0
    try:
        # Bind authenticated listeners before starting either upstream service.
        servers = [
            AuthenticatedServer(8888, 8889, token, "/health"),
            AuthenticatedServer(9999, 9998, token, "/api/health"),
        ]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        child = subprocess.Popen(
            ["/app/start-all.sh"], env=upstream_environment(token), start_new_session=True
        )
        while not stop.wait(0.5):
            if child.poll() is not None:
                exit_code = child.returncode or 1
                break
    finally:
        if child is not None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                child.wait(timeout=35)
            # Kill descendants even if their original shell has already exited.
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        for server in servers:
            server.shutdown()
            server.server_close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
