"""Authenticated, bounded evaluation center; no arbitrary commands or graders."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import current_user
from octop.api.routers.longterm import service as longterm_service
from octop.infra.db.repos.longterm import AuthorityRevoked, RevisionConflict
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.longterm.evaluation_center import EvaluationCenterService
from octop.infra.longterm.service import LongtermService
from octop.infra.users.identity import User

router = APIRouter(prefix="/tenants/{tenant_id}/evaluations", tags=["evaluations"])


def service(longterm: LongtermService = Depends(longterm_service)) -> EvaluationCenterService:
    return longterm.evaluation_center


@contextmanager
def errors() -> Iterator[None]:
    try:
        yield
    except RevisionConflict as exc:
        raise OctopError(
            ErrorCode.LONGTERM_CONFLICT,
            "evaluation revision conflict",
            details={"reason": str(exc)},
        ) from exc
    except AuthorityRevoked as exc:
        raise OctopError(ErrorCode.FORBIDDEN, "evaluation authority revoked") from exc
    except (ValueError, OSError) as exc:
        raise OctopError(
            ErrorCode.LONGTERM_INVALID,
            "invalid evaluation data",
            details={"reason": type(exc).__name__},
        ) from exc


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationResourceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class EvaluationSuiteResponse(EvaluationResourceResponse):
    suite_id: str
    name: str
    version: int
    revision: int
    cases: list[dict[str, Any]]


class EvaluationSuitesResponse(EvaluationResourceResponse):
    suites: list[EvaluationSuiteResponse]
    builtin: EvaluationSuiteResponse


class EvaluationRunResponse(EvaluationResourceResponse):
    run_id: str
    status: str
    revision: int
    aggregate: dict[str, Any]


class EvaluationRunsResponse(EvaluationResourceResponse):
    runs: list[EvaluationRunResponse]


class EvaluationAttemptResponse(EvaluationResourceResponse):
    attempt_id: str
    case_id: str
    repeat_index: int
    status: str


class EvaluationBadcaseResponse(EvaluationResourceResponse):
    badcase_id: str
    status: str
    revision: int


class EvaluationBadcasesResponse(EvaluationResourceResponse):
    badcases: list[EvaluationBadcaseResponse]


class EvaluationComparisonResponse(EvaluationResourceResponse):
    comparison_id: str
    gate: str
    release_eligible: bool
    reasons: list[str]


class SuiteBody(StrictBody):
    space_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    split: Literal["capability", "regression", "holdout"] = "capability"
    cases: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    suite_id: str | None = Field(default=None, max_length=160)
    expected_revision: int = Field(default=0, ge=0, strict=True)


class RunBody(StrictBody):
    space_id: str = Field(min_length=1, max_length=160)
    suite_id: str = "builtin-contract-v1"
    suite_version: int | None = Field(default=None, ge=1, strict=True)
    subject: dict[str, Any] = Field(default_factory=lambda: {"kind": "builtin"})
    repeats: int = Field(default=3, ge=1, le=5, strict=True)
    mode: Literal["local", "temporal"] = "local"
    max_total_tokens: int = Field(default=100000, ge=1, le=2000000, strict=True)
    max_parallel: int = Field(default=2, ge=1, le=4, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=160)


class CommandBody(StrictBody):
    command: Literal["pause", "resume", "cancel"]
    expected_revision: int = Field(ge=1, strict=True)
    idempotency_key: str | None = Field(default=None, max_length=160)


class ComparisonBody(StrictBody):
    baseline_run_id: str = Field(min_length=1, max_length=160)
    candidate_run_id: str = Field(min_length=1, max_length=160)
    min_improvement: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)


class BadcaseBody(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)
    status: Literal["triaged", "fixed", "verified"] | None = None
    root_cause: str | None = Field(default=None, max_length=1000)
    fix_ref: str | None = Field(default=None, max_length=1000)
    verification_run_id: str | None = Field(default=None, max_length=160)


class RegressionBody(StrictBody):
    expected_revision: int = Field(ge=1, strict=True)
    name: str | None = Field(default=None, max_length=200)


class ImportBody(StrictBody):
    space_id: str = Field(min_length=1, max_length=160)
    run_dirs: list[str] = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=160)


@router.get(
    "/suites",
    response_model=EvaluationSuitesResponse,
    summary="List private versioned evaluation suites",
    description="Includes a no-model local DAG contract suite; all saved suites are private to the space owner.",
)
def suites(
    tenant_id: str,
    space_id: str = Query(min_length=1),
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.suites(tenant_id, user, space_id)


@router.get(
    "/suites/{suite_id}",
    response_model=EvaluationSuiteResponse,
    summary="Read an immutable evaluation suite version",
    description="An explicit version reads its frozen cases and graders, including superseded versions.",
)
def suite(
    tenant_id: str,
    suite_id: str,
    version: int | None = Query(default=None, ge=1),
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.suite(tenant_id, user, suite_id, version)


@router.post(
    "/suites",
    response_model=EvaluationSuiteResponse,
    summary="Create or version an evaluation suite",
    description="Validates bounded cases and allowlisted deterministic rules; existing versions remain immutable.",
)
def save_suite(
    tenant_id: str,
    body: SuiteBody,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.save_suite(tenant_id, user, body.model_dump(exclude_none=True))


@router.get(
    "/runs",
    response_model=EvaluationRunsResponse,
    summary="List private evaluation campaigns",
    description="Returns planned denominators and coverage alongside native, synthetic and external result classifications.",
)
def runs(
    tenant_id: str,
    space_id: str = Query(min_length=1),
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.runs(tenant_id, user, space_id)


@router.post(
    "/runs",
    response_model=EvaluationRunResponse,
    summary="Start a bounded native evaluation campaign",
    description="Freezes suite and subject, reserves the full token budget, and dispatches each case and repeat through ordinary durable Octop tasks. Builtin mode makes no model calls.",
)
async def create_run(
    tenant_id: str,
    body: RunBody,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.create_run(tenant_id, user, body.model_dump(exclude_none=True))


@router.get(
    "/runs/{run_id}",
    response_model=EvaluationRunResponse,
    summary="Read campaign results and attempts",
    description="Keeps task goal verification separate from evaluation outcomes and exposes every planned trial.",
)
def run(
    tenant_id: str,
    run_id: str,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.run(tenant_id, user, run_id)


@router.get(
    "/runs/{run_id}/attempts/{attempt_id}",
    response_model=EvaluationAttemptResponse,
    summary="Read a trial and its real execution trace",
    description="Trace rows come from durable dispatch completion events and retain their evidence-chain references.",
)
def attempt(
    tenant_id: str,
    run_id: str,
    attempt_id: str,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.attempt(tenant_id, user, run_id, attempt_id)


@router.post(
    "/runs/{run_id}/commands",
    response_model=EvaluationRunResponse,
    summary="Pause, resume or cancel an evaluation",
    description="Revision-checked campaign controls propagate to native tasks and are recoverable after interruption.",
)
async def command(
    tenant_id: str,
    run_id: str,
    body: CommandBody,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.command(tenant_id, user, run_id, body.model_dump(exclude_none=True))


@router.post(
    "/comparisons",
    response_model=EvaluationComparisonResponse,
    summary="Compare paired baseline and candidate trials",
    description="Requires compatible complete matrices, checks case regressions and safety, and never authorizes publication from synthetic or imported results.",
)
def compare(
    tenant_id: str,
    body: ComparisonBody,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.compare(tenant_id, user, body.model_dump())


@router.get(
    "/badcases",
    response_model=EvaluationBadcasesResponse,
    summary="List evaluation failures for diagnosis",
    description="Failure records preserve the original case, trial and failed rules for the repair and reassessment loop.",
)
def badcases(
    tenant_id: str,
    space_id: str = Query(min_length=1),
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.badcases(tenant_id, user, space_id)


@router.patch(
    "/badcases/{badcase_id}",
    response_model=EvaluationBadcaseResponse,
    summary="Record a diagnosis or verify a repair",
    description="Verified status requires a complete passing reassessment of the same frozen case contract.",
)
def badcase(
    tenant_id: str,
    badcase_id: str,
    body: BadcaseBody,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.update_badcase(tenant_id, user, badcase_id, body.model_dump(exclude_none=True))


@router.post(
    "/badcases/{badcase_id}/regression",
    response_model=EvaluationSuiteResponse,
    summary="Graduate a verified repair into a regression suite",
    description="Creates a frozen regression case without changing the original experiment or its score.",
)
def regression(
    tenant_id: str,
    badcase_id: str,
    body: RegressionBody,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.graduate_badcase(tenant_id, user, badcase_id, body.model_dump(exclude_none=True))


@router.post(
    "/imports",
    response_model=EvaluationRunResponse,
    summary="Import recorded CommerceAgentBench trials",
    description="Reads only configured private result directories, retains verifier provenance and harness identity, and makes no benchmark or model calls.",
)
def import_cab(
    tenant_id: str,
    body: ImportBody,
    user: User = Depends(current_user),
    svc: EvaluationCenterService = Depends(service),
) -> dict[str, Any]:
    with errors():
        return svc.import_cab(tenant_id, user, body.model_dump())
