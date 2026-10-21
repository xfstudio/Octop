import { request } from "../request";
import type { ManagedComponentId } from "./componentManagement";

export interface SessionOperation {
  component_id: ManagedComponentId;
  operation: string;
  input_example: Record<string, unknown>;
  task_required?: boolean;
  node_required?: boolean;
  project_required?: boolean;
}

export interface OperationSessionResult {
  source: string;
  component_id: ManagedComponentId;
  operation: string;
  output: unknown;
  verified: boolean;
  usage: { total_tokens: number };
  draft_only: true;
  promotion_allowed: false;
}

export interface OperationSession {
  session_id: string;
  component_id: ManagedComponentId;
  operation: string;
  input: Record<string, unknown>;
  task_id: string | null;
  node_id: string | null;
  snapshot: {
    console?: {
      url: string | null;
      kind: "internal" | "external";
      label: string;
      available: boolean;
    };
    [key: string]: unknown;
  };
  status: "created" | "ready" | "committed" | "cancelled";
  revision: number;
  candidate: OperationSessionResult | null;
  result: OperationSessionResult | null;
  result_digest: string | null;
  source: string | null;
  created_at: number;
  updated_at: number;
}

export interface SessionCreateBody {
  component_id: ManagedComponentId;
  operation: string;
  input: Record<string, unknown>;
  task_id?: string;
  node_id?: string;
  idempotency_key: string;
}

const base = (tenantId: string, spaceId: string) =>
  `/longterm/tenants/${encodeURIComponent(
    tenantId,
  )}/spaces/${encodeURIComponent(spaceId)}/operation-sessions`;
const path = (tenantId: string, spaceId: string, sessionId: string) =>
  `${base(tenantId, spaceId)}/${encodeURIComponent(sessionId)}`;

export const operationSessionsApi = {
  operations: (signal?: AbortSignal) =>
    request<{ operations: SessionOperation[] }>(
      "/longterm/operation-session-operations",
      { signal },
    ),
  list: (tenantId: string, spaceId: string, signal?: AbortSignal) =>
    request<{ sessions: OperationSession[] }>(base(tenantId, spaceId), {
      signal,
    }),
  get: (
    tenantId: string,
    spaceId: string,
    sessionId: string,
    signal?: AbortSignal,
  ) =>
    request<OperationSession>(path(tenantId, spaceId, sessionId), { signal }),
  create: (
    tenantId: string,
    spaceId: string,
    body: SessionCreateBody,
    signal?: AbortSignal,
  ) =>
    request<OperationSession>(base(tenantId, spaceId), {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    }),
  action: (
    tenantId: string,
    spaceId: string,
    sessionId: string,
    action: "readback" | "import" | "commit" | "cancel",
    body: {
      expected_revision: number;
      idempotency_key: string;
      result?: unknown;
      result_digest?: string;
    },
    signal?: AbortSignal,
  ) =>
    request<OperationSession>(
      `${path(tenantId, spaceId, sessionId)}/${action}`,
      { method: "POST", body: JSON.stringify(body), signal },
    ),
};
