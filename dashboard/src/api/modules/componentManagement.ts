import { request } from "../request";

export type ManagedComponentId =
  | "octop"
  | "semantica"
  | "beatdesign"
  | "commerceagentbench"
  | "rna"
  | "hindsight"
  | "temporal";

export interface ComponentConfigField {
  key: string;
  type: "string" | "boolean";
  secret: boolean;
  required: boolean;
  value?: string | boolean;
  configured: boolean;
}

export interface ManagedComponent {
  id: ManagedComponentId;
  name: string;
  configured: boolean;
  healthy: boolean | null;
  status: string;
  detail: string;
  checked_at: number | null;
  fields: ComponentConfigField[];
  actions: string[];
}

export interface ComponentCatalog {
  can_manage: boolean;
  scope: "server";
  components: ManagedComponent[];
}

export interface ComponentConfiguration {
  component_id: ManagedComponentId;
  values: Record<string, string | boolean>;
  secret_configured: Record<string, boolean>;
  fields: ComponentConfigField[];
}

export interface ComponentConfigurationPatch {
  values: Record<string, string | boolean>;
  secrets: Record<string, string>;
  clear_secrets: string[];
}

export interface ComponentConnectionCheck {
  component_id: ManagedComponentId;
  configured: boolean;
  healthy: boolean;
  checked_at: number;
  checks: { code: string; ok: boolean }[];
}

export type ComponentLifecycleAction = "start" | "stop";

export interface ComponentRuntime {
  component_id: ManagedComponentId;
  name: string;
  mode: "embedded" | "compose" | "process" | "external";
  state:
    | "enabled"
    | "disabled"
    | "running"
    | "stopped"
    | "partial"
    | "starting"
    | "stopping"
    | "unknown";
  enabled: boolean;
  revision: number;
  can_start: boolean;
  can_stop: boolean;
  reason: string | null;
  services: { name: string; state: string; health: string | null }[];
  console: {
    url: string | null;
    kind: "internal" | "external";
    label: "integration" | "native";
    available: boolean;
  };
}

export class ComponentLifecycleError extends Error {
  readonly status: number | undefined;
  readonly reason: string;

  constructor(status: number | undefined, reason: string) {
    super("Component lifecycle request failed");
    this.name = "ComponentLifecycleError";
    this.status = status;
    this.reason = reason;
  }
}

function lifecycleError(cause: unknown): ComponentLifecycleError {
  const message = cause instanceof Error ? cause.message : "";
  const statusText = /^Request failed: (\d+)\b/.exec(message)?.[1];
  let reason = "lifecycle_failed";
  try {
    const body = JSON.parse(message.slice(message.indexOf(" - ") + 3)) as {
      reason?: string;
      detail?: { reason?: string };
      error?: { reason?: string; details?: { reason?: string } };
    };
    const code =
      body.detail?.reason ??
      body.error?.details?.reason ??
      body.error?.reason ??
      body.reason;
    if (typeof code === "string" && /^[a-z0-9_]+$/.test(code)) reason = code;
  } catch {
    // Keep a safe fallback; raw server and subprocess output stays hidden.
  }
  return new ComponentLifecycleError(
    statusText ? Number(statusText) : undefined,
    reason,
  );
}

export type ProjectComponentId = "beatdesign" | "rna";
export type ProjectReadOperation =
  | "project"
  | "canvas"
  | "assets"
  | "skills"
  | "generation_models"
  | "generation_history"
  | "editor"
  | "editor_diagnostics"
  | "genes"
  | "observability";

export interface ComponentProjectBinding {
  component_id: ProjectComponentId;
  tenant_id: string;
  project_id: string | null;
  revision: number;
  can_manage: boolean;
}

export interface ComponentProjectRead {
  source: string;
  operation: ProjectReadOperation;
  project_id: string;
  result: Record<string, unknown>;
}

const base = "/longterm/integrations";
const componentPath = (id: ManagedComponentId) =>
  `${base}/${encodeURIComponent(id)}`;
const projectPath = (tenantId: string, id: ProjectComponentId) =>
  `/longterm/tenants/${encodeURIComponent(
    tenantId,
  )}/integrations/${id}/project`;

export const componentManagementApi = {
  catalog: (signal?: AbortSignal) =>
    request<ComponentCatalog>(`${base}/catalog`, { signal }),
  configuration: (id: ManagedComponentId, signal?: AbortSignal) =>
    request<ComponentConfiguration>(`${componentPath(id)}/config`, { signal }),
  save: (
    id: ManagedComponentId,
    patch: ComponentConfigurationPatch,
    signal?: AbortSignal,
  ) =>
    request<ComponentConfiguration>(`${componentPath(id)}/config`, {
      method: "PUT",
      body: JSON.stringify(patch),
      signal,
    }),
  check: (id: ManagedComponentId, signal?: AbortSignal) =>
    request<ComponentConnectionCheck>(`${componentPath(id)}/check`, {
      method: "POST",
      signal,
    }),
  runtime: (signal?: AbortSignal) =>
    request<{ components: ComponentRuntime[] }>(`${base}/runtime`, { signal }),
  lifecycle: async (
    id: ManagedComponentId,
    action: ComponentLifecycleAction,
    expectedRevision: number,
    signal?: AbortSignal,
  ): Promise<ComponentRuntime> => {
    try {
      return await request<ComponentRuntime>(`${componentPath(id)}/lifecycle`, {
        method: "POST",
        body: JSON.stringify({
          action,
          confirmed: true,
          expected_revision: expectedRevision,
        }),
        signal,
      });
    } catch (cause) {
      throw lifecycleError(cause);
    }
  },
  project: (tenantId: string, id: ProjectComponentId, signal?: AbortSignal) =>
    request<ComponentProjectBinding>(projectPath(tenantId, id), { signal }),
  bindProject: (
    tenantId: string,
    id: ProjectComponentId,
    body: { project_id: string | null; expected_revision: number },
    signal?: AbortSignal,
  ) =>
    request<ComponentProjectBinding>(projectPath(tenantId, id), {
      method: "PUT",
      body: JSON.stringify(body),
      signal,
    }),
  readProject: (
    tenantId: string,
    spaceId: string,
    id: ProjectComponentId,
    operation: ProjectReadOperation,
    signal?: AbortSignal,
  ) =>
    request<ComponentProjectRead>(
      `/longterm/tenants/${encodeURIComponent(
        tenantId,
      )}/spaces/${encodeURIComponent(spaceId)}/integrations/${id}/read`,
      {
        method: "POST",
        body: JSON.stringify({ operation }),
        signal,
      },
    ),
};
