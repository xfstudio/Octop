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
