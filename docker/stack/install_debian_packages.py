"""Build-only HTTPS downloader for packages from APT's signed local indexes.

Run apt-get update first. Existing archive cache entries are reused only after
their size and SHA256 match the authenticated APT package plan.
"""

import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ARCHIVES = Path("/var/cache/apt/archives")
APT_OPTIONS = ["-o", "Acquire::Retries=3", "-o", "Acquire::https::Timeout=30"]
PLAN_ROW = re.compile(
    r"^'([^']+)' ([A-Za-z0-9][A-Za-z0-9+_.:~%=-]*\.deb) "
    r"([0-9]+) SHA256:([a-fA-F0-9]{64})$"
)
RANGE_THRESHOLD = 8 * 1024 * 1024
RANGE_SIZE = 4 * 1024 * 1024
RANGE_WORKERS = 4


class RangeUnsupported(Exception):
    """The server ignored the Range request and returned the complete resource."""


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise ValueError("Package redirects must use HTTPS.")
        return super().redirect_request(request, response, code, message, headers, new_url)


def run_apt(arguments, *, capture=False):
    result = subprocess.run(
        ["apt-get", *APT_OPTIONS, *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout


def cached(target, size, expected_hash):
    try:
        info = target.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != size:
        return False
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_hash


def copy_response(response, output, size, deadline):
    received = 0
    while chunk := response.read(64 * 1024):
        received += len(chunk)
        if received > size:
            raise ValueError("Response exceeds its expected size.")
        if time.monotonic() > deadline:
            raise TimeoutError("Package download exceeded its time limit.")
        output.write(chunk)
    if received != size:
        raise ValueError("Response does not match its expected size.")


def download_range(uri, partial, size, start, end):
    for attempt in range(1, 4):
        try:
            opener = build_opener(HTTPSRedirectHandler())
            request = Request(
                uri,
                headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"},
            )
            deadline = time.monotonic() + 600
            with opener.open(request, timeout=30) as response:
                if response.status == 200:
                    raise RangeUnsupported()
                if response.status != 206:
                    raise ValueError(f"Range request returned HTTP {response.status}.")
                if response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                    raise ValueError("Content-Range does not match the requested signed range.")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Range response must use identity encoding.")
                length = response.headers.get("Content-Length")
                if length is not None and length != str(end - start + 1):
                    raise ValueError("Range Content-Length does not match its expected size.")
                with partial.open("r+b") as output:
                    output.seek(start)
                    copy_response(response, output, end - start + 1, deadline)
            return
        except RangeUnsupported:
            raise
        except Exception:
            if attempt == 3:
                raise
            time.sleep(attempt)


def download_contents(uri, partial, size):
    if size >= RANGE_THRESHOLD:
        # The first segment also probes support. A malformed 206 must fail;
        # only an explicit 200 response permits the ordinary-download fallback.
        try:
            download_range(uri, partial, size, 0, min(RANGE_SIZE, size) - 1)
        except RangeUnsupported:
            print("[apt-download] server ignored Range; using full download", flush=True)
        else:
            ranges = [
                (start, min(start + RANGE_SIZE, size) - 1)
                for start in range(RANGE_SIZE, size, RANGE_SIZE)
            ]
            with ThreadPoolExecutor(max_workers=RANGE_WORKERS) as executor:
                futures = [
                    executor.submit(download_range, uri, partial, size, start, end)
                    for start, end in ranges
                ]
                for future in futures:
                    future.result()
            return
    opener = build_opener(HTTPSRedirectHandler())
    # Permit slow mirrors while bounding the fallback (at least 16 KiB/s,
    # plus startup allowance). Socket inactivity is still limited to 30 s.
    deadline = time.monotonic() + max(600, 300 + size / (16 * 1024))
    with opener.open(Request(uri, headers={"Accept-Encoding": "identity"}), timeout=30) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        with partial.open("wb") as output:
            copy_response(response, output, size, deadline)


def download(package):
    uri, filename, size, expected_hash = package
    target = ARCHIVES / filename
    if cached(target, size, expected_hash):
        print(f"[apt-download] cache verified {filename} ({size} bytes)", flush=True)
        return
    # A stale or partial cache entry must never satisfy --no-download install.
    target.unlink(missing_ok=True)
    for attempt in range(1, 4):
        partial = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=filename + ".",
                suffix=".musae-partial",
                dir=ARCHIVES,
                delete=False,
            ) as output:
                partial = Path(output.name)
            download_contents(uri, partial, size)
            if not cached(partial, size, expected_hash):
                raise ValueError("Package size or SHA256 does not match signed APT metadata.")
            partial.chmod(0o644)
            os.replace(partial, target)
            print(f"[apt-download] verified {filename} ({size} bytes)", flush=True)
            return
        except Exception as error:
            if attempt == 3:
                raise RuntimeError(f"Cannot verify {filename}: {error}") from error
            print(
                f"[apt-download] retry {attempt}/3: {filename}: {error}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(attempt)
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)


def main():
    packages = list(dict.fromkeys(sys.argv[1:]))
    if not packages or any(
        re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", value) is None for value in packages
    ):
        raise ValueError("Provide explicit Debian package names.")
    plan = run_apt(
        [
            "-o",
            "Acquire::ForceHash=sha256",
            "--print-uris",
            "--yes",
            "--no-install-recommends",
            "install",
            *packages,
        ],
        capture=True,
    )
    downloads = []
    filenames = set()
    for line in plan.splitlines():
        if not line.startswith("'"):
            continue
        match = PLAN_ROW.fullmatch(line)
        if match is None:
            raise ValueError(f"APT did not provide a SHA256 package plan: {line}")
        uri, filename, size, expected_hash = match.groups()
        if urlsplit(uri).scheme != "https":
            raise ValueError("Package URL must use HTTPS.")
        if filename in filenames:
            raise ValueError("APT package filenames must be unique.")
        filenames.add(filename)
        downloads.append((uri, filename, int(size), expected_hash.lower()))
    ARCHIVES.mkdir(parents=True, exist_ok=True)
    print(
        f"[apt-download] {len(downloads)} signed packages, up to 8 concurrent downloads",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(download, downloads))
    run_apt(["--yes", "--no-install-recommends", "--no-download", "install", *packages])


if __name__ == "__main__":
    main()
