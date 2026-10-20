"""Provision persistent stack credentials and launch Octop without logging them."""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit


def provision(root: Path, config: Path) -> None:
    """Reuse credentials across restarts; never silently rotate an existing copy."""
    names = ("octop", "postgres", "beatdesign", "rna", "semantica", "hindsight", "benchmark")
    for name in names:
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    pairs = {
        "database-password": ("octop", "postgres"),
        "bootstrap-password": ("postgres",),
        "admin-password": ("octop",),
        **{f"{name}-token": ("octop", name) for name in names[2:]},
    }
    for filename, owners in pairs.items():
        paths = [root / owner / filename for owner in owners]
        existing = {p.read_text(encoding="utf-8").strip() for p in paths if p.exists()}
        if len(existing) > 1 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", v) for v in existing
        ):
            raise ValueError(f"inconsistent or invalid persistent credential: {filename}")
        value = next(iter(existing)) if existing else "M" + secrets.token_hex(24) + "7"
        for path in paths:
            if not path.exists():
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(value + "\n")
            path.chmod(0o600)
    if os.name == "posix":
        for name in names[2:]:
            for path in (root / name).iterdir():
                os.chown(path, 1000, 1000)
            os.chown(root / name, 1000, 1000)
    project = os.environ.get("STACK_PROJECT_NAME", "musae")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", project):
        raise ValueError("invalid stack project name")
    host = os.environ.get("STACK_PUBLIC_HOST", "localhost")
    parsed = urlsplit("http://" + host)
    if (
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host)
        or not parsed.hostname
        or parsed.netloc != host
        or parsed.port
        or parsed.path
        or parsed.username
    ):
        raise ValueError("STACK_PUBLIC_HOST must be a hostname or IPv4 address without a port")
    components: dict[str, dict[str, object]] = {"octop": {"mode": "embedded"}}
    defaults = {
        "temporal": 8233,
        "beatdesign": 3020,
        "rna": 4317,
        "semantica": 8800,
        "hindsight": 9998,
        "commerceagentbench": 8091,
    }
    for name, default in defaults.items():
        port = int(os.environ.get(f"STACK_{name.upper()}_PORT", str(default)))
        if not 1 <= port <= 65535:
            raise ValueError("invalid public console port")
        components[name] = {
            "mode": "docker",
            "project_name": project,
            "services": ["temporal", "worker"] if name == "temporal" else [name],
            "console_url": f"http://{host}:{port}",
        }
    config.mkdir(parents=True, exist_ok=True)
    config.chmod(0o755)
    target = config / "component-runtime.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "components": components}, indent=2) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o644)
    temporary.replace(target)


def launch(mode: str) -> None:
    root = Path(os.environ.get("STACK_SECRET_DIR", "/run/stack-secrets"))
    mapping = {
        "OCTOP_DATABASE_PASSWORD": "database-password",
        "OCTOP_BEATDESIGN_TOKEN": "beatdesign-token",
        "OCTOP_RNA_TOKEN": "rna-token",
        "OCTOP_HINDSIGHT_API_KEY": "hindsight-token",
    }
    if mode == "octop":
        mapping["OCTOP_DEFAULT_PASSWORD"] = "admin-password"
    for variable, filename in mapping.items():
        value = (root / filename).read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"missing persistent credential: {filename}")
        os.environ[variable] = value
    if mode in {"worker", "worker-health"}:
        args = [sys.executable, "-m", "octop.infra.longterm.worker"]
        if mode == "worker-health":
            args.append("--healthcheck")
        os.execv(sys.executable, args)
    elif mode == "octop":
        for variable in ("OCTOP_BENCH_RESULTS_ROOT", "OCTOP_EVOLUTION_REPORTS_ROOT"):
            if directory := os.environ.get(variable):
                Path(directory).mkdir(parents=True, exist_ok=True)
        os.execv("/usr/local/bin/docker-entrypoint.sh", ["docker-entrypoint.sh"])
    else:
        raise ValueError("invalid stack launcher mode")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["init"]:
            provision(Path("/secrets"), Path("/stack-config"))
            print("[stack] Persistent credentials and component bindings are ready.")
        else:
            launch(sys.argv[1])
    except (OSError, ValueError, IndexError) as exc:
        # Exception text only identifies a deployment file, never its contents.
        print(f"[stack] Startup refused: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
