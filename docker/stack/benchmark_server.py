"""Authenticated, read-only Octop report wrapper around CommerceAgentBench.

The upstream project supplies the installed CLI, not this HTTP interface.
No HTTP handler submits jobs, executes the CLI, or calls a model provider.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import stat
import sys
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import quote, unquote, urlsplit

REPORT_NAMES = {"report.html", "summary.json", "summary.md", "manifest.json", "final_reward.json"}
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_REPORTS = 500
MAX_ENTRIES = 10000


def configured_token() -> str:
    value = os.getenv("BENCHMARK_API_TOKEN", "").strip()
    filename = os.getenv("BENCHMARK_API_TOKEN_FILE")
    if not value and filename:
        with Path(filename).open(encoding="utf-8") as stream:
            value = stream.read(8193).strip()
    if not 32 <= len(value) <= 8192 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError("a provisioned report token of at least 32 characters is required")
    return value


def open_report(root: Path, relative: str) -> BinaryIO:
    """Open beneath root without following symlinks, including parent components.

    This service runs in the Linux image, where dir_fd and O_NOFOLLOW are available.
    """
    path = PurePosixPath(relative)
    if (
        not path.parts
        or path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or "\\" in relative
        or "\x00" in relative
        or path.name not in REPORT_NAMES
    ):
        raise FileNotFoundError
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)
    stream = os.fdopen(descriptor, "rb")
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_REPORT_BYTES:
        stream.close()
        raise FileNotFoundError
    return stream


def report_index(root: Path) -> tuple[list[dict[str, object]], bool]:
    result: list[dict[str, object]] = []
    scanned = 0
    for directory, directories, filenames in os.walk(root, followlinks=False):
        scanned += len(directories)
        if scanned > MAX_ENTRIES:
            return result, True
        base = Path(directory)
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".") and not (base / name).is_symlink()
        )
        for name in sorted(filenames):
            scanned += 1
            if scanned > MAX_ENTRIES:
                return result, True
            if name not in REPORT_NAMES:
                continue
            relative = (base / name).relative_to(root).as_posix()
            try:
                with open_report(root, relative) as stream:
                    metadata = os.fstat(stream.fileno())
            except OSError:
                continue
            result.append(
                {"path": relative, "bytes": metadata.st_size, "modified_at": metadata.st_mtime}
            )
            if len(result) >= MAX_REPORTS:
                return result, True
    return result, False


class ReportServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, token: str, port: int) -> None:
        self.results_root = root
        self.token = token
        self.upstream_version = version("commerce-agent-bench")
        super().__init__(("0.0.0.0", port), ReportHandler)


class ReportHandler(BaseHTTPRequestHandler):
    server: ReportServer
    server_version = "OctopBenchReports/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # Do not log report paths, request URLs, or authentication material.
        pass

    def _headers(
        self, status: int, content_type: str, size: int, *, authenticate: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "base-uri 'none'; form-action 'none'",
        )
        if authenticate:
            self.send_header(
                "WWW-Authenticate", 'Basic realm="Octop benchmark reports", charset="UTF-8"'
            )
        self.end_headers()

    def _json(self, status: int, value: object, *, authenticate: bool = False) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(
            status, "application/json; charset=utf-8", len(body), authenticate=authenticate
        )
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        import hmac

        candidate = self.headers.get("X-API-Key", "")
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            candidate = authorization[7:]
        elif authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                username, separator, password = decoded.partition(":")
                candidate = password if separator and username == "admin" else ""
            except (ValueError, binascii.Error):
                candidate = ""
        return bool(candidate) and hmac.compare_digest(
            candidate.encode("utf-8"), self.server.token.encode("ascii")
        )

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok", "mode": "idle"})
            return
        if not self._authenticated():
            self._json(401, {"error": "authentication_required"}, authenticate=True)
            return
        if path == "/api/info":
            self._json(
                200,
                {
                    "name": "Octop CommerceAgentBench report wrapper",
                    "upstream": "commerce-agent-bench",
                    "upstream_version": self.server.upstream_version,
                    "cli_available": shutil.which("commerce-agent-bench") is not None,
                    "mode": "idle",
                    "job_submission_enabled": False,
                },
            )
        elif path == "/api/jobs":
            self._json(200, {"managed_jobs": [], "job_submission_enabled": False})
        elif path in {"/", "/api/reports"}:
            reports, truncated = report_index(self.server.results_root)
            if path == "/api/reports":
                self._json(200, {"reports": reports, "truncated": truncated})
            else:
                links = "".join(
                    '<li><a href="/reports/'
                    + quote(str(report["path"]), safe="/")
                    + '">'
                    + escape(str(report["path"]))
                    + "</a></li>"
                    for report in reports
                )
                body = (
                    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    "<title>CommerceAgentBench 运行报告</title><body>"
                    "<h1>CommerceAgentBench 运行报告</h1>"
                    "<p>Octop 的只读报告包装服务。真实 CLI 已安装；当前未派发任务。"
                    "这不是上游独立 Web 控制台，也不观测外部 CLI 进程。</p><ul>"
                    + links
                    + "</ul><p>"
                    + ("结果已截断。" if truncated else "")
                    + "</p></body></html>"
                ).encode("utf-8")
                self._headers(200, "text/html; charset=utf-8", len(body))
                self.wfile.write(body)
        elif path.startswith("/reports/"):
            relative = unquote(path[len("/reports/") :])
            try:
                stream = open_report(self.server.results_root, relative)
            except (OSError, ValueError):
                self._json(404, {"error": "report_not_found"})
                return
            with stream:
                size = os.fstat(stream.fileno()).st_size
                suffix = PurePosixPath(relative).suffix
                content_type = {
                    ".html": "text/html; charset=utf-8",
                    ".json": "application/json",
                }.get(suffix, "text/plain; charset=utf-8")
                self._headers(200, content_type, size)
                remaining = size
                while remaining:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        self._json(405, {"error": "read_only_service"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def main() -> None:
    try:
        token = configured_token()
        root = Path(os.getenv("BENCHMARK_RESULTS_ROOT", "/results")).resolve(strict=True)
        port = int(os.getenv("BENCHMARK_PORT", "8090"))
        if not root.is_dir() or not 1 <= port <= 65535:
            raise ValueError
    except (OSError, ValueError):
        sys.exit(
            "Benchmark reports require an existing results directory and a protected API token."
        )
    with ReportServer(root, token, port) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
