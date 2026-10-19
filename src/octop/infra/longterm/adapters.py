"""Opt-in adapters against the checked-in upstream APIs.

No network connection or optional import happens at module import. Each result
describes the service that actually answered; an unavailable adapter raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .execution import canonical_json, parse_utc


class IntegrationUnavailable(RuntimeError):
    """An explicitly requested upstream integration is unavailable."""


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        result = value.model_dump(mode="json", by_alias=True)
    elif hasattr(value, "to_dict"):
        result = value.to_dict()
    else:
        raise TypeError("integration response is not a JSON object")
    if not isinstance(result, dict):
        raise TypeError("integration response is not a JSON object")
    return result


def tenant_namespace(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id is required")
    return hashlib.sha256(tenant_id.encode()).hexdigest()


class SemanticaAdapter:
    """Semantic provenance index over an already durable local evidence record.

    A separate SQLite index is used for each tenant. Semantica is not treated as
    an external notarization service or a substitute for the authoritative log.
    """

    def __init__(
        self,
        storage_root: Path,
        *,
        manager_factory: Callable[..., Any] | None = None,
        authorization_check: Callable[[], None] | None = None,
    ) -> None:
        self.storage_root = storage_root
        self._manager_factory = manager_factory
        self._authorization_check = authorization_check

    async def record(self, event: dict[str, Any], *, evidence_ref: str) -> dict[str, Any]:
        if not evidence_ref:
            raise ValueError("durable local evidence_ref is required")
        tenant_id, task_id = event["tenant_id"], event["task_id"]
        namespace = tenant_namespace(tenant_id)
        event_digest = hashlib.sha256(canonical_json(event).encode()).hexdigest()
        entity_id = f"octop:{namespace}:{event_digest}"

        def index() -> dict[str, Any]:
            factory = self._manager_factory
            if factory is None:
                try:
                    factory = importlib.import_module(
                        "semantica.provenance.manager"
                    ).ProvenanceManager
                except ImportError as exc:
                    raise IntegrationUnavailable(
                        "install the semantica integration dependency"
                    ) from exc
            self.storage_root.mkdir(parents=True, exist_ok=True)
            manager = factory(storage_path=str(self.storage_root / f"{namespace}.sqlite3"))
            if self._authorization_check is not None:
                self._authorization_check()
            entry = manager.track_entity(
                entity_id=entity_id,
                source=evidence_ref,
                entity_type="agent_execution_event",
                activity_id=f"octop:{task_id}:{event.get('phase', 'event')}",
                agent_id=str(event.get("agent_id", "octop")),
                bundle_id=f"octop:{namespace}:{task_id}",
                metadata={
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "event_digest": event_digest,
                    "evidence_ref": evidence_ref,
                    "event": event,
                },
            )
            # Upstream track_entity may swallow storage errors. Read back the digest.
            stored = manager.get_provenance(entity_id)
            if (
                entry is None
                or not isinstance(stored, dict)
                or stored.get("metadata", {}).get("event_digest") != event_digest
            ):
                raise IntegrationUnavailable("semantica provenance persistence was not confirmed")
            return {
                "source": "semantica",
                "indexed": True,
                "entity_id": entity_id,
                "evidence_ref": evidence_ref,
                "event_digest": event_digest,
            }

        return await asyncio.get_running_loop().run_in_executor(None, index)


class HindsightAdapter:
    """Tenant bank isolation, qualified retains, and fail-closed read-time TTL.

    This upstream SDK has no native TTL argument. Expiry lives in document
    metadata and is enforced here. ``delete_expired`` explicitly deletes expired
    documents discovered by recall; it does not claim a server-side TTL scheduler.
    Observation synthesis is excluded because it can outlive source metadata.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        client: Any = None,
        clock: Callable[[], datetime] | None = None,
        authorization_check: Callable[[], None] | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self._client = client
        self.clock = clock or (lambda: datetime.now(UTC))
        self._authorization_check = authorization_check

    @staticmethod
    def bank_id(tenant_id: str) -> str:
        return f"octop-{tenant_namespace(tenant_id)}"

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                factory = importlib.import_module("hindsight_client").Hindsight
            except ImportError as exc:
                raise IntegrationUnavailable("install hindsight-client to use memory") from exc
            self._client = factory(base_url=self.base_url, api_key=self.api_key, timeout=30.0)
        return self._client

    async def retain(
        self,
        *,
        tenant_id: str,
        task_id: str,
        content: str,
        evidence_refs: list[str],
        verified: bool,
        ttl_seconds: int,
        consent: bool = False,
    ) -> dict[str, Any]:
        if not consent:
            raise ValueError("memory write requires explicit tenant consent")
        if (
            verified is not True
            or not evidence_refs
            or any(not isinstance(ref, str) or not ref for ref in evidence_refs)
        ):
            raise ValueError("only verified, evidence-backed memories qualify for retain")
        if not content.strip() or len(content) > 50000:
            raise ValueError("memory content must contain 1..50000 characters")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= 31536000
        ):
            raise ValueError("ttl_seconds must be in 1..31536000")
        namespace = tenant_namespace(tenant_id)
        digest = hashlib.sha256(
            canonical_json([task_id, content, evidence_refs]).encode()
        ).hexdigest()
        document_id = f"octop-{digest}"
        expires_at = (self.clock() + timedelta(seconds=ttl_seconds)).isoformat()
        client = self._get_client()
        if self._authorization_check is not None:
            self._authorization_check()
        response = _object(
            await client.aretain(
                bank_id=self.bank_id(tenant_id),
                content=content,
                document_id=document_id,
                metadata={
                    "octop_tenant": namespace,
                    "octop_task_id": task_id,
                    "octop_expires_at": expires_at,
                    "octop_evidence_refs": canonical_json(evidence_refs),
                },
                tags=[f"octop-tenant:{namespace}", "octop-verified"],
                update_mode="replace",
                retain_async=False,
            )
        )
        if response.get("success") is not True:
            raise IntegrationUnavailable("hindsight retain was not acknowledged")
        return {
            "source": "hindsight",
            "retained": True,
            "bank_id": self.bank_id(tenant_id),
            "document_id": document_id,
            "expires_at": expires_at,
            "ttl_enforcement": "adapter_read_filter",
        }

    async def recall(self, *, tenant_id: str, query: str, max_tokens: int = 4096) -> dict[str, Any]:
        if not 1 <= max_tokens <= 16384:
            raise ValueError("max_tokens must be in 1..16384")
        namespace = tenant_namespace(tenant_id)
        client = self._get_client()
        if self._authorization_check is not None:
            self._authorization_check()
        response = _object(
            await client.arecall(
                bank_id=self.bank_id(tenant_id),
                query=query,
                types=["world", "experience"],
                max_tokens=max_tokens,
                tags=[f"octop-tenant:{namespace}", "octop-verified"],
                tags_match="all_strict",
                prefer_observations=False,
            )
        )
        memories: list[dict[str, Any]] = []
        expired: set[str] = set()
        for raw in response.get("results", []):
            item = _object(raw)
            metadata = item.get("metadata") or {}
            if metadata.get("octop_tenant") != namespace:
                continue
            try:
                expiry = parse_utc(metadata["octop_expires_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if expiry <= self.clock():
                if item.get("document_id"):
                    expired.add(item["document_id"])
                continue
            if not metadata.get("octop_evidence_refs"):
                continue
            memories.append(item)
        return {
            "source": "hindsight",
            "bank_id": self.bank_id(tenant_id),
            "results": memories,
            "expired_document_ids": sorted(expired),
            "ttl_enforcement": "adapter_read_filter",
        }

    async def delete_expired(self, *, tenant_id: str, recall_result: dict[str, Any]) -> int:
        if recall_result.get("bank_id") != self.bank_id(tenant_id):
            raise ValueError("recall bank mismatch")
        count = 0
        # The caller should persist and schedule this cleanup explicitly.
        for document_id in recall_result.get("expired_document_ids", []):
            if not isinstance(document_id, str) or not document_id.startswith("octop-"):
                raise ValueError("invalid memory document id")
            if self._authorization_check is not None:
                self._authorization_check()
            await self._get_client().documents.delete_document(self.bank_id(tenant_id), document_id)
            count += 1
        return count

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class BeatDesignAdapter:
    """Use BeatDesign's actual MCP Command Kernel, with a fixed tenant project map.

    BeatDesign itself is single-user. Deploy distinct instances/data directories
    for mutually untrusted tenants; project binding alone is not upstream RBAC.
    It is a skill/canvas/media integration, not a generic agent runtime.
    """

    PROJECT_TOOLS = frozenset(
        {
            "bdesign_project_get",
            "bdesign_asset_list",
            "bdesign_asset_get",
            "bdesign_asset_import",
            "bdesign_asset_extract_frame",
            "bdesign_canvas_get",
            "bdesign_canvas_view",
            "bdesign_canvas_search",
            "bdesign_canvas_apply",
            "bdesign_canvas_continue_from_tail",
            "bdesign_generation_submit",
            "bdesign_generation_status",
            "bdesign_generation_history",
            "bdesign_editor_get",
            "bdesign_editor_edit",
            "bdesign_editor_import_srt",
            "bdesign_editor_render",
            "bdesign_editor_snapshot",
            "bdesign_editor_diagnostics",
            "bdesign_editor_view",
            "bdesign_editor_history",
        }
    )
    GLOBAL_TOOLS = frozenset(
        {
            "bdesign_skill_list",
            "bdesign_skill_get",
            "bdesign_generation_models",
            "bdesign_generation_model_get",
        }
    )
    READ_TOOLS = frozenset(
        {
            "bdesign_project_get",
            "bdesign_asset_list",
            "bdesign_asset_get",
            "bdesign_canvas_get",
            "bdesign_canvas_view",
            "bdesign_canvas_search",
            "bdesign_generation_status",
            "bdesign_generation_history",
            "bdesign_editor_get",
            "bdesign_editor_snapshot",
            "bdesign_editor_diagnostics",
            "bdesign_editor_view",
            "bdesign_editor_history",
        }
    )

    def __init__(
        self, endpoint: str, tenant_projects: dict[str, str], *, token: str | None = None
    ) -> None:
        self.endpoint = endpoint
        self.tenant_projects = dict(tenant_projects)
        self.token = token

    async def call_tool(
        self, tenant_id: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if tool not in self.PROJECT_TOOLS | self.GLOBAL_TOOLS:
            raise ValueError("BeatDesign tool is outside the scoped integration catalog")
        if tenant_id not in self.tenant_projects:
            raise ValueError("tenant has no BeatDesign project binding")
        args = dict(arguments)
        if tool in self.PROJECT_TOOLS:
            project = self.tenant_projects[tenant_id]
            if args.get("projectId", project) != project:
                raise ValueError("cross-tenant BeatDesign project access")
            args["projectId"] = project
        try:
            session_type = importlib.import_module("mcp").ClientSession
            transport = importlib.import_module("mcp.client.streamable_http").streamablehttp_client
        except ImportError as exc:
            raise IntegrationUnavailable("the MCP Python SDK is required") from exc
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        async with (
            transport(self.endpoint, headers=headers) as (read, write, _),
            session_type(read, write) as session,
        ):
            await session.initialize()
            result = _object(await session.call_tool(tool, arguments=args))
        if result.get("isError"):
            raise IntegrationUnavailable("BeatDesign rejected the MCP command")
        return {"source": "beatdesign_mcp", "tool": tool, "result": result}

    async def execute(self, node: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        tool = node.get("tool") or str(node.get("skill", "")).removeprefix("beatdesign.")
        args = dict(node.get("input", {}))
        if tool in self.PROJECT_TOOLS - self.READ_TOOLS:
            if node["risk"] == "R0":
                raise ValueError("BeatDesign writes cannot be classified R0")
            key = task["execution"]["idempotency_key"]
            # Canvas/editor command tools accept the shared metadata envelope.
            if tool in {"bdesign_canvas_apply", "bdesign_editor_edit"}:
                args.update(commandId=key, idempotencyKey=key)
        return await self.call_tool(task["tenant_id"], tool, args)


class RnaAdapter:
    """Read evolution evidence or explicitly review an existing RNA v3 gene.

    There is no invented 'evolve' endpoint. Local candidates are managed by the
    Octop EvolutionService; gene activation is a separately authorized mutation.
    """

    def __init__(
        self,
        base_url: str,
        tenant_projects: dict[str, str],
        *,
        token: str | None = None,
        http_client: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant_projects = dict(tenant_projects)
        self.token = token
        self._http_client = http_client

    def _project_path(self, tenant_id: str) -> str:
        project = self.tenant_projects.get(tenant_id)
        if not project:
            raise ValueError("tenant has no RNA project binding")
        return f"/api/v2/projects/{quote(project, safe='')}/genes"

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if self._http_client is not None:
            response = await self._http_client.request(
                method, self.base_url + path, json=payload, headers=headers
            )
        else:
            try:
                httpx = importlib.import_module("httpx")
            except ImportError as exc:
                raise IntegrationUnavailable("httpx is required for the RNA adapter") from exc
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                response = await client.request(
                    method, self.base_url + path, json=payload, headers=headers
                )
        response.raise_for_status()
        return _object(response.json())

    async def genes(self, tenant_id: str) -> dict[str, Any]:
        return await self._request("GET", self._project_path(tenant_id))

    async def observability(self, tenant_id: str) -> dict[str, Any]:
        return await self._request("GET", self._project_path(tenant_id) + "/observability")

    async def review_gene(
        self,
        tenant_id: str,
        gene_id: str,
        action: str,
        *,
        explicitly_authorized: bool = False,
        text: str | None = None,
    ) -> dict[str, Any]:
        if not explicitly_authorized:
            raise ValueError("RNA gene mutations require explicit authorization")
        if action not in {"confirm", "revoke", "rewrite", "deep_check"}:
            raise ValueError("unknown RNA gene action")
        payload: dict[str, Any] = {"action": action}
        if action == "rewrite":
            if not text or not text.strip() or len(text) > 1000:
                raise ValueError("rewrite requires 1..1000 characters")
            payload["text"] = text
        elif text is not None:
            raise ValueError("text is only accepted for rewrite")
        return await self._request(
            "POST", self._project_path(tenant_id) + "/" + quote(gene_id, safe=""), payload
        )
