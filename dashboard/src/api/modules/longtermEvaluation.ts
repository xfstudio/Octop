import { request } from "../request";

export interface EvaluationSuite {
  suite_id: string;
  name: string;
  split: "capability" | "regression" | "holdout";
  version: number;
  revision: number;
  digest: string;
  cases: Record<string, unknown>[];
}

export interface EvaluationAttempt {
  attempt_id: string;
  case_id: string;
  repeat_index: number;
  native_task_id?: string;
  status: string;
  result?: Record<string, unknown>;
  revision: number;
  trace?: Record<string, unknown>[];
  evidence_valid?: boolean;
  task?: {
    task_id: string;
    status: string;
    plan_digest?: string;
    result?: Record<string, unknown>;
    checkpoint?: Record<string, unknown>;
  };
}

export interface EvaluationBadcase {
  badcase_id: string;
  run_id: string;
  attempt_id: string;
  case_id: string;
  status: "open" | "triaged" | "fixed" | "verified";
  root_cause?: string;
  fix_ref?: string;
  verification_run_id?: string;
  failed_rules?: unknown[];
  revision: number;
}

export interface EvaluationRun {
  run_id: string;
  status: string;
  revision: number;
  suite: EvaluationSuite;
  subject: {
    kind: "builtin" | "source_task";
    digest: string;
    task_id?: string;
  };
  source_class: "synthetic" | "native" | "external_import" | "synthetic_import";
  harness: string;
  repeats: number;
  mode: string;
  aggregate: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  attempts?: EvaluationAttempt[];
  badcases?: EvaluationBadcase[];
}

export interface EvaluationComparison {
  comparison_id: string;
  gate: "GO" | "NO_GO" | "INCOMPLETE";
  release_eligible: boolean;
  reasons: unknown[];
  mean_improvement: number | null;
  case_pairs: unknown[];
  baseline_run_id: string;
  candidate_run_id: string;
}

const base = (tenantId: string) =>
  `/longterm/tenants/${encodeURIComponent(tenantId)}/evaluations`;
const runPath = (tenantId: string, runId: string) =>
  `${base(tenantId)}/runs/${encodeURIComponent(runId)}`;
const query = (spaceId: string) => `?space_id=${encodeURIComponent(spaceId)}`;

export const longtermEvaluationApi = {
  suites: (tenantId: string, spaceId: string, signal?: AbortSignal) =>
    request<{ suites: EvaluationSuite[]; builtin: EvaluationSuite }>(
      `${base(tenantId)}/suites${query(spaceId)}`,
      { signal },
    ),
  saveSuite: (
    tenantId: string,
    body: {
      space_id: string;
      name: string;
      split: "capability" | "regression" | "holdout";
      cases: Record<string, unknown>[];
      suite_id?: string;
      expected_revision: number;
    },
    signal?: AbortSignal,
  ) =>
    request<EvaluationSuite>(`${base(tenantId)}/suites`, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  runs: (tenantId: string, spaceId: string, signal?: AbortSignal) =>
    request<{ runs: EvaluationRun[] }>(
      `${base(tenantId)}/runs${query(spaceId)}`,
      { signal },
    ),
  run: (tenantId: string, runId: string, signal?: AbortSignal) =>
    request<EvaluationRun>(runPath(tenantId, runId), { signal }),
  attempt: (
    tenantId: string,
    runId: string,
    attemptId: string,
    signal?: AbortSignal,
  ) =>
    request<EvaluationAttempt>(
      `${runPath(tenantId, runId)}/attempts/${encodeURIComponent(attemptId)}`,
      { signal },
    ),
  start: (
    tenantId: string,
    body: {
      space_id: string;
      suite_id: string;
      suite_version?: number;
      subject: { kind: "builtin" } | { kind: "source_task"; task_id: string };
      repeats: number;
      mode: "local" | "temporal";
      max_total_tokens: number;
      max_parallel: number;
      idempotency_key: string;
    },
    signal?: AbortSignal,
  ) =>
    request<EvaluationRun>(`${base(tenantId)}/runs`, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  command: (
    tenantId: string,
    runId: string,
    body: {
      command: "pause" | "resume" | "cancel";
      expected_revision: number;
      idempotency_key: string;
    },
    signal?: AbortSignal,
  ) =>
    request<EvaluationRun>(`${runPath(tenantId, runId)}/commands`, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  compare: (
    tenantId: string,
    body: {
      baseline_run_id: string;
      candidate_run_id: string;
      min_improvement: number;
    },
    signal?: AbortSignal,
  ) =>
    request<EvaluationComparison>(`${base(tenantId)}/comparisons`, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  badcases: (tenantId: string, spaceId: string, signal?: AbortSignal) =>
    request<{ badcases: EvaluationBadcase[] }>(
      `${base(tenantId)}/badcases${query(spaceId)}`,
      { signal },
    ),
  updateBadcase: (
    tenantId: string,
    badcaseId: string,
    body: {
      expected_revision: number;
      status: "triaged" | "fixed";
      root_cause: string;
      fix_ref: string;
      verification_run_id?: string;
    },
    signal?: AbortSignal,
  ) =>
    request<EvaluationBadcase>(
      `${base(tenantId)}/badcases/${encodeURIComponent(badcaseId)}`,
      { method: "PATCH", body: JSON.stringify(body), signal },
    ),
  regression: (
    tenantId: string,
    badcaseId: string,
    body: { expected_revision: number; name?: string },
    signal?: AbortSignal,
  ) =>
    request<EvaluationSuite>(
      `${base(tenantId)}/badcases/${encodeURIComponent(badcaseId)}/regression`,
      { method: "POST", body: JSON.stringify(body), signal },
    ),
  importRuns: (
    tenantId: string,
    body: { space_id: string; run_dirs: string[]; idempotency_key: string },
    signal?: AbortSignal,
  ) =>
    request<EvaluationRun>(`${base(tenantId)}/imports`, {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
};
