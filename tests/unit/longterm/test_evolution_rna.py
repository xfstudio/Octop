"""Exercise the Node boundary and, when checked out, the real RNA SDK transport."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from octop.infra.longterm.evolution import RnaSdkAdapter

_BASELINE = {
    "projectId": "project-a",
    "strategy": "check sources",
    "skills": [{"id": "verify", "instructions": "verify sources"}],
    "assignments": {"skillIds": ["verify"], "toolIds": [], "mcpServerIds": []},
}
_INPUT = {
    "project": {"id": "project-a"},
    "baseline": _BASELINE,
    "evidence": [{"id": "evidence-1", "summary": "A required source was missing"}],
    "model": {
        "providerId": "local-test",
        "modelId": "test-model",
        "protocol": "openai",
        "baseUrl": "http://127.0.0.1:1/v1",
        "contextWindow": 8192,
        "maxOutputTokens": 1000,
    },
    "api_key_env": "OCTOP_TEST_RNA_KEY",
}


def _sdk(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "sdk"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(
        '{"name":"@rna-agent/sdk","version":"test"}', encoding="utf-8"
    )
    (root / "src" / "evolution.mjs").write_text(source, encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_missing_credentials_block_without_a_synthetic_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OCTOP_TEST_RNA_KEY", raising=False)
    result = await RnaSdkAdapter(tmp_path).propose(**_INPUT)
    assert result == {
        "source": "rna_dev",
        "status": "blocked",
        "reason": "configured_model_credentials_required",
    }


@pytest.mark.asyncio
async def test_embedded_credentials_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_TEST_RNA_KEY", "synthetic-rna-test-credential")
    root = _sdk(tmp_path, "export async function proposeEvolution() {}")
    request = {**_INPUT, "model": {**_INPUT["model"], "apiKey": "do-not-send"}}
    with pytest.raises(ValueError, match="environment reference"):
        await RnaSdkAdapter(root).propose(**request)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runtime is optional")
@pytest.mark.asyncio
async def test_sdk_timeout_terminates_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_TEST_RNA_KEY", "synthetic-rna-test-credential")
    root = _sdk(
        tmp_path,
        "export async function proposeEvolution() { return await new Promise(resolve => setTimeout(resolve, 60000)); }",
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await RnaSdkAdapter(root).propose(**_INPUT, timeout_seconds=0.1)
    assert asyncio.get_running_loop().time() - started < 10


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runtime is optional")
@pytest.mark.asyncio
async def test_provider_error_does_not_reflect_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-rna-test-credential"
    monkeypatch.setenv("OCTOP_TEST_RNA_KEY", secret)
    root = _sdk(
        tmp_path,
        "export async function proposeEvolution() { throw new Error(process.env.RNA_ADAPTER_API_KEY); }",
    )
    with pytest.raises(RuntimeError) as caught:
        await RnaSdkAdapter(root).propose(**_INPUT)
    assert secret not in str(caught.value)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node runtime is optional")
@pytest.mark.asyncio
async def test_real_rna_sdk_proposes_via_a_local_provider(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = repo_root.parent / "rna_dev" / "packages" / "sdk"
    if not (sdk / "src" / "evolution.mjs").is_file():
        pytest.skip("Real rna_dev checkout is an optional local integration")
    monkeypatch.setenv("OCTOP_TEST_RNA_KEY", "synthetic-rna-test-credential")
    received: list[dict[str, Any]] = []
    proposal = {
        "summary": "核实输入",
        "hypothesis": "先验证来源可降低遗漏",
        "strategy": "先验证来源，再形成答案",
        "skillUpdates": [
            {
                "skillId": "verify",
                "instructions": "verify required sources before answering",
                "reason": "missing source evidence",
            }
        ],
        "expectedImprovement": "减少来源遗漏",
        "riskNotes": ["尚未进行固定基准评测"],
    }

    async def provider(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            received.append(json.loads(await reader.readexactly(length)))
            frames = [
                {
                    "choices": [
                        {
                            "delta": {"content": json.dumps(proposal, ensure_ascii=False)},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 50, "total_tokens": 80},
                },
            ]
            body = (
                "".join(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames)
                + "data: [DONE]\n\n"
            ).encode("utf-8")
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(provider, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        request = {**_INPUT, "model": {**_INPUT["model"], "baseUrl": f"http://127.0.0.1:{port}/v1"}}
        result = await RnaSdkAdapter(sdk).propose(**request, timeout_seconds=15)
    assert result["status"] == "proposed"
    assert result["proposal"] == proposal
    assert result["source"] == "rna_dev"
    assert result["usageReported"] is True
    assert len(result["baseline_digest"]) == 64
    assert len(received) == 1
    assert received[0].get("tools", []) == []
    assert "score" not in result
