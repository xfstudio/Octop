import { request } from "../request";

export interface WorkflowNode {
  id: string;
  agent_id?: string;
  skill?: string;
  depends_on: string[];
  risk: "R0" | "R1" | "R2" | "R3";
  input: Record<string, unknown>;
  beatdesign_card_id?: string;
  resources?: string[];
  timeout_seconds?: number;
}

export interface WorkflowDefinition {
  nodes: WorkflowNode[];
  max_cycles?: number;
  max_actions?: number;
  max_parallel?: number;
  timeout_seconds?: number;
}

export interface WorkspaceConfig {
  schema_version: 1;
  workspace_name: string;
  workflow: WorkflowDefinition;
  defaults: { mode: "local" | "temporal"; max_cycles: number };
}

export interface Tenant {
  tenant_id: string;
  name: string;
  role: string;
  revision: number;
}

export interface TenantSpace {
  space_id: string;
  name: string;
  revision: number;
}

export interface TenantMember {
  user_id: number;
  role: "owner" | "admin" | "member" | "viewer";
}

export interface RemoteConfiguration {
  space_id: string;
  revision: number;
  config: WorkspaceConfig | Record<string, never>;
  device_id?: string;
  updated_at?: string;
}

export interface LongtermTask {
  task_id: string;
  tenant_id: string;
  space_id: string;
  goal: string;
  status: string;
  revision: number;
  workflow: WorkflowDefinition;
  mode?: string;
  checkpoint?: Record<string, unknown>;
  result?: unknown;
  plan_digest?: string;
  created_at?: string;
  updated_at?: string;
}

export interface EvidenceEvent {
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string | number;
  digest: string;
  previous_digest?: string;
}

export interface TaskEvidence {
  events: EvidenceEvent[];
  valid: boolean;
  head_digest: string;
}

export interface IntegrationStatus {
  id: string;
  name: string;
  status: string;
  detail: string;
}

export type TaskCommand = "start" | "pause" | "resume" | "cancel" | "approve";
export type TaskOperation = "evaluate" | "evolution" | "memory" | "provenance";
const base = "/longterm";
const tenantPath = (tenantId: string) =>
  `${base}/tenants/${encodeURIComponent(tenantId)}`;
const taskPath = (tenantId: string, taskId: string) =>
  `${tenantPath(tenantId)}/tasks/${encodeURIComponent(taskId)}`;
const spacePath = (tenantId: string, spaceId: string) =>
  `${tenantPath(tenantId)}/spaces/${encodeURIComponent(spaceId)}/config`;

export const longtermApi = {
  tenants: () => request<{ tenants: Tenant[] }>(`${base}/tenants`),
  createTenant: (name: string) =>
    request<Tenant>(`${base}/tenants`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  importBeatDesign: (tenantId: string, document: Record<string, unknown>) =>
    request<{ workflow: WorkflowDefinition }>(
      `${tenantPath(tenantId)}/workflow/import`,
      { method: "POST", body: JSON.stringify({ document }) },
    ),
  members: (tenantId: string) =>
    request<{ members: TenantMember[] }>(`${tenantPath(tenantId)}/members`),
  setMember: (
    tenantId: string,
    userId: number,
    role: "admin" | "member" | "viewer" | null,
  ) =>
    request<{ ok: boolean }>(`${tenantPath(tenantId)}/members`, {
      method: "PUT",
      body: JSON.stringify({ user_id: userId, role }),
    }),
  spaces: (tenantId: string) =>
    request<{ spaces: TenantSpace[] }>(`${tenantPath(tenantId)}/spaces`),
  createSpace: (tenantId: string, name: string) =>
    request<TenantSpace>(`${tenantPath(tenantId)}/spaces`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  configuration: (tenantId: string, spaceId: string) =>
    request<RemoteConfiguration>(spacePath(tenantId, spaceId)),
  uploadConfiguration: (
    tenantId: string,
    spaceId: string,
    body: {
      expected_revision: number;
      device_id: string;
      config: WorkspaceConfig;
    },
  ) =>
    request<RemoteConfiguration>(spacePath(tenantId, spaceId), {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  tasks: (tenantId: string) =>
    request<{ tasks: LongtermTask[] }>(`${tenantPath(tenantId)}/tasks`),
  task: (tenantId: string, taskId: string) =>
    request<LongtermTask>(taskPath(tenantId, taskId)),
  createTask: (
    tenantId: string,
    body: {
      space_id: string;
      goal: string;
      workflow: WorkspaceConfig["workflow"];
      mode: "local" | "temporal";
      max_cycles: number;
      idempotency_key: string;
    },
  ) =>
    request<LongtermTask>(`${tenantPath(tenantId)}/tasks`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  command: (
    tenantId: string,
    taskId: string,
    body: {
      command: TaskCommand;
      expected_revision: number;
      plan_digest?: string;
      node_id?: string;
    },
  ) =>
    request<LongtermTask>(`${taskPath(tenantId, taskId)}/commands`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  evidence: (tenantId: string, taskId: string) =>
    request<TaskEvidence>(`${taskPath(tenantId, taskId)}/evidence`),
  taskOperation: (
    tenantId: string,
    taskId: string,
    operation: TaskOperation,
    body: Record<string, unknown>,
  ) =>
    request<unknown>(`${taskPath(tenantId, taskId)}/${operation}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  integrations: () =>
    request<{ integrations: IntegrationStatus[] }>(`${base}/integrations`),
};
