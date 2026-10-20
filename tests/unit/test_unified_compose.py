"""Unified stack startup shares only the intended credentials and runtime bindings."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from octop.infra.longterm.component_runtime import _read_manifest

REPO = Path(__file__).resolve().parents[2]
CONSUMERS = {"octop", "postgres", "beatdesign", "rna", "semantica", "hindsight", "benchmark"}
COMPONENTS = {
    "octop",
    "temporal",
    "beatdesign",
    "rna",
    "semantica",
    "hindsight",
    "commerceagentbench",
}


@pytest.fixture
def bootstrap(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "octop_test_stack_bootstrap", REPO / "docker" / "stack" / "stack_bootstrap.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, "chown", lambda *args: None, raising=False)
    for name in list(os.environ):
        if name.startswith("STACK_"):
            monkeypatch.delenv(name, raising=False)
    for name in {
        "OCTOP_DATABASE_PASSWORD",
        "OCTOP_BEATDESIGN_TOKEN",
        "OCTOP_RNA_TOKEN",
        "OCTOP_HINDSIGHT_API_KEY",
        "OCTOP_DEFAULT_PASSWORD",
    }:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return module


@pytest.fixture
def compose() -> dict[str, Any]:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


def _credentials(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text() for path in root.glob("*/*") if path.is_file()
    }


def _mounts(service: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    result = {}
    for mount in service.get("volumes", []):
        if isinstance(mount, str):
            parts = mount.split(":")
            result[parts[0]] = (parts[1], len(parts) > 2 and parts[2] == "ro")
        else:
            result[mount["source"]] = (mount["target"], bool(mount.get("read_only")))
    return result


def test_bootstrap_generates_unique_credentials_and_recovers_missing_copies_without_rotation(
    bootstrap: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, config = tmp_path / "secrets", tmp_path / "config"
    bootstrap.provision(root, config)
    assert {path.name for path in root.iterdir()} == CONSUMERS
    before = _credentials(root)
    values = {value.strip() for value in before.values()}
    assert len(values) == 8 and all(
        re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value) for value in values
    )
    assert (root / "octop/database-password").read_bytes() == (
        root / "postgres/database-password"
    ).read_bytes()
    for name in CONSUMERS - {"octop", "postgres"}:
        assert (root / name / f"{name}-token").read_bytes() == (
            root / "octop" / f"{name}-token"
        ).read_bytes()
        assert {path.name for path in (root / name).iterdir()} == {f"{name}-token"}
    if os.name == "posix":
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in root.iterdir())
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.glob("*/*"))
    monkeypatch.setattr(
        bootstrap.secrets, "token_hex", lambda *args: pytest.fail("restart must reuse credentials")
    )
    bootstrap.provision(root, config)
    assert _credentials(root) == before
    (root / "hindsight/hindsight-token").unlink()
    bootstrap.provision(root, config)
    assert _credentials(root) == before
    assert not (config / "component-runtime.tmp").exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("replacement", ["X" * 50, "", "short", "contains whitespace " * 3])
def test_bootstrap_refuses_conflicting_or_invalid_persistent_copies(
    bootstrap: ModuleType, tmp_path: Path, replacement: str
) -> None:
    root, config = tmp_path / "secrets", tmp_path / "config"
    bootstrap.provision(root, config)
    conflicting = root / "postgres/database-password"
    conflicting.write_text(replacement + "\n")
    before = _credentials(root)
    with pytest.raises(ValueError, match="database-password") as error:
        bootstrap.provision(root, config)
    if replacement:
        assert replacement not in str(error.value)
    assert _credentials(root) == before


def test_bootstrap_runtime_manifest_matches_compose_console_defaults_and_project_scope(
    bootstrap: ModuleType,
    compose: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACK_PROJECT_NAME", "private_test-project1")
    monkeypatch.setenv("STACK_PUBLIC_HOST", "127.0.0.1")
    root, config = tmp_path / "secrets", tmp_path / "config"
    bootstrap.provision(root, config)
    target = config / "component-runtime.json"
    monkeypatch.setenv("OCTOP_COMPONENT_RUNTIME_FILE", str(target))
    bindings = _read_manifest()
    assert set(bindings) == COMPONENTS
    assert bindings["octop"] == {"mode": "embedded"}
    init_env = compose["services"]["init"]["environment"]
    for name, binding in bindings.items():
        if name == "octop":
            continue
        assert binding["mode"] == "docker" and binding["project_name"] == "private_test-project1"
        assert binding["services"] == (["temporal", "worker"] if name == "temporal" else [name])
        default = re.fullmatch(r"\$\{[^:}]+:-([0-9]+)\}", init_env[f"STACK_{name.upper()}_PORT"])
        assert default is not None
        assert binding["console_url"] == f"http://127.0.0.1:{default[1]}"
    raw = target.read_text()
    assert all(value.strip() not in raw for value in _credentials(root).values())


def test_bootstrap_runtime_manifest_respects_console_port_overrides(
    bootstrap: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports = {name: 21000 + index for index, name in enumerate(sorted(COMPONENTS - {"octop"}))}
    for name, port in ports.items():
        monkeypatch.setenv(f"STACK_{name.upper()}_PORT", str(port))
    config = tmp_path / "config"
    bootstrap.provision(tmp_path / "secrets", config)
    bindings = json.loads((config / "component-runtime.json").read_text())["components"]
    assert {name: urlsplit(bindings[name]["console_url"]).port for name in ports} == ports


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("STACK_PROJECT_NAME", "Other.Project"),
        ("STACK_PUBLIC_HOST", "user:secret@example.test"),
        ("STACK_PUBLIC_HOST", "localhost:8088"),
        ("STACK_PUBLIC_HOST", "example.test/path"),
        ("STACK_TEMPORAL_PORT", "0"),
        ("STACK_TEMPORAL_PORT", "65536"),
    ],
)
def test_bootstrap_refuses_invalid_runtime_targets(
    bootstrap: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)
    config = tmp_path / "config"
    with pytest.raises(ValueError):
        bootstrap.provision(tmp_path / "secrets", config)
    assert not (config / "component-runtime.json").exists()


@pytest.mark.parametrize("mode", ["octop", "worker", "worker-health"])
def test_bootstrap_launch_loads_persistent_credentials_before_exec_or_healthcheck(
    bootstrap: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root = tmp_path / "secrets"
    bootstrap.provision(root, tmp_path / "config")
    monkeypatch.setenv("STACK_SECRET_DIR", str(root / "octop"))
    report_roots = [tmp_path / "results", tmp_path / "reports"]
    for variable, directory in zip(
        ("OCTOP_BENCH_RESULTS_ROOT", "OCTOP_EVOLUTION_REPORTS_ROOT"), report_roots, strict=True
    ):
        monkeypatch.setenv(variable, str(directory))
    calls: list[tuple[str, list[str]]] = []

    def execute(executable: str, args: list[str]) -> None:
        calls.append((executable, args))

    monkeypatch.setattr(bootstrap.os, "execv", execute)
    bootstrap.launch(mode)
    for variable, filename in {
        "OCTOP_DATABASE_PASSWORD": "database-password",
        "OCTOP_BEATDESIGN_TOKEN": "beatdesign-token",
        "OCTOP_RNA_TOKEN": "rna-token",
        "OCTOP_HINDSIGHT_API_KEY": "hindsight-token",
    }.items():
        assert os.environ[variable] == (root / "octop" / filename).read_text().strip()
    if mode == "octop":
        assert all(directory.is_dir() for directory in report_roots)
        assert (
            os.environ["OCTOP_DEFAULT_PASSWORD"]
            == (root / "octop/admin-password").read_text().strip()
        )
        assert calls == [("/usr/local/bin/docker-entrypoint.sh", ["docker-entrypoint.sh"])]
    else:
        expected = [bootstrap.sys.executable, "-m", "octop.infra.longterm.worker"]
        if mode == "worker-health":
            expected.append("--healthcheck")
        assert calls == [(bootstrap.sys.executable, expected)]
        assert "OCTOP_DEFAULT_PASSWORD" not in os.environ


def test_unified_compose_starts_all_services_and_exposes_only_loopback_ports(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]
    assert set(services) == COMPONENTS | {"init", "postgres", "worker"}
    assert all(not service.get("profiles") for service in services.values())
    for service in services.values():
        assert service.get("network_mode") != "host" and not service.get("privileged")
        assert all(
            isinstance(port, str) and port.startswith("127.0.0.1:")
            for port in service.get("ports", [])
        )
    assert services["init"]["restart"] == "no"
    assert services["octop"]["ulimits"]["nofile"]["soft"] >= 65535
    assert (
        services["octop"]["ulimits"]["nofile"]["hard"]
        >= services["octop"]["ulimits"]["nofile"]["soft"]
    )


def test_unified_compose_isolates_secret_volumes_and_shares_only_octop_credentials(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]
    expected = {name: f"{name}_secrets" for name in CONSUMERS - {"benchmark"}}
    expected.update(worker="octop_secrets", commerceagentbench="benchmark_secrets")
    secret_volumes = {f"{name}_secrets" for name in CONSUMERS}
    assert secret_volumes <= set(compose["volumes"])
    for service, volume in expected.items():
        mounts = _mounts(services[service])
        assert set(mounts) & secret_volumes == {volume}
        assert mounts[volume] == ("/run/stack-secrets", True)
    assert set(_mounts(services["init"])) & secret_volumes == secret_volumes
    assert not any(read_only for _, read_only in _mounts(services["init"]).values())
    assert _mounts(services["octop"])["octop_data"] == _mounts(services["worker"])["octop_data"]
    assert _mounts(services["octop"])["stack_config"] == ("/stack-config", True)
    assert _mounts(services["worker"])["stack_config"] == ("/stack-config", True)
    socket_users = {
        name for name, service in services.items() if "/var/run/docker.sock" in _mounts(service)
    }
    assert socket_users == {"octop"}
    assert not (set(_mounts(services["temporal"])) & secret_volumes)


def test_unified_compose_waits_for_bootstrap_database_and_temporal_and_wraps_worker_health(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]
    for name in {
        "postgres",
        "octop",
        "beatdesign",
        "rna",
        "semantica",
        "hindsight",
        "commerceagentbench",
    }:
        assert services[name]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    for name in ("postgres", "temporal"):
        assert services["octop"]["depends_on"][name]["condition"] == "service_healthy"
    for name in ("octop", "temporal"):
        assert services["worker"]["depends_on"][name]["condition"] == "service_healthy"
    assert services["worker"]["command"] == ["worker"]
    health = services["worker"]["healthcheck"]["test"]
    assert health[0] == "CMD" and health[-1] == "worker-health"
    assert any(value.endswith("/stack_bootstrap.py") for value in health)
    for name in ("octop", "worker"):
        env = services[name]["environment"]
        assert (
            env["OCTOP_DATABASE_DRIVER"] == "postgresql"
            and env["OCTOP_DATABASE_HOST"] == "postgres"
        )
        assert "OCTOP_DATABASE_PASSWORD" not in env
    shared = [
        "OCTOP_DATABASE_NAME",
        "OCTOP_DATABASE_USER",
        "OCTOP_TEMPORAL_ADDRESS",
        "OCTOP_TEMPORAL_NAMESPACE",
        "OCTOP_TEMPORAL_TASK_QUEUE",
    ]
    assert {key: services["octop"]["environment"][key] for key in shared} == {
        key: services["worker"]["environment"][key] for key in shared
    }


def test_unified_compose_sources_are_overridable_build_contexts(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert services["octop"]["build"] == services["worker"]["build"]
    expected = {
        "octop": {"rna_src": "RNA_SOURCE", "semantica_src": "SEMANTICA_SOURCE"},
        "beatdesign": {"beatdesign_src": "BEATDESIGN_SOURCE"},
        "rna": {"rna_src": "RNA_SOURCE"},
        "semantica": {"semantica_src": "SEMANTICA_SOURCE"},
        "commerceagentbench": {"benchmark_src": "BENCHMARK_SOURCE"},
    }
    for name, contexts in expected.items():
        for context, variable in contexts.items():
            assert services[name]["build"]["additional_contexts"][context].startswith(
                "${" + variable + ":-../"
            )
