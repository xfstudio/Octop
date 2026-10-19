import type { WorkspaceConfig, WorkflowNode } from "../../api/modules/longterm";

export const LOCAL_WORKSPACES_KEY = "octop:longterm:workspaces:v1";

export function localIdentifier(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export interface LocalWorkspace {
  id: string;
  config: WorkspaceConfig;
  updated_at: string;
  sync?: {
    tenant_id: string;
    space_id: string;
    revision: number;
    synced_at: string;
  };
}

export interface LocalWorkspaceState {
  schema_version: 1;
  device_id: string;
  active_workspace_id: string;
  workspaces: LocalWorkspace[];
}

export function newLocalWorkspace(name: string): LocalWorkspace {
  return {
    id: localIdentifier(),
    config: {
      schema_version: 1,
      workspace_name: name,
      workflow: { nodes: [] },
      defaults: { mode: "local", max_cycles: 8 },
    },
    updated_at: new Date().toISOString(),
  };
}

export function createLocalWorkspaceState(name: string): LocalWorkspaceState {
  const workspace = newLocalWorkspace(name);
  return {
    schema_version: 1,
    device_id: localIdentifier(),
    active_workspace_id: workspace.id,
    workspaces: [workspace],
  };
}

export function isWorkspaceConfig(value: unknown): value is WorkspaceConfig {
  if (!value || typeof value !== "object") return false;
  const config = value as Partial<WorkspaceConfig>;
  return (
    config.schema_version === 1 &&
    typeof config.workspace_name === "string" &&
    Array.isArray(config.workflow?.nodes) &&
    config.workflow.nodes.every(
      (node) =>
        node &&
        typeof node.id === "string" &&
        Array.isArray(node.depends_on) &&
        node.depends_on.every((id) => typeof id === "string") &&
        ["R0", "R1", "R2", "R3"].includes(node.risk) &&
        node.input !== null &&
        typeof node.input === "object" &&
        !Array.isArray(node.input),
    ) &&
    (config.defaults?.mode === "local" ||
      config.defaults?.mode === "temporal") &&
    Number.isInteger(config.defaults?.max_cycles) &&
    (config.defaults?.max_cycles ?? 0) > 0
  );
}

/** Only known portable fields can cross the sync boundary. Authentication is never read here. */
export function portableConfiguration(
  config: WorkspaceConfig,
): WorkspaceConfig {
  return {
    schema_version: 1,
    workspace_name: config.workspace_name,
    defaults: {
      mode: config.defaults.mode,
      max_cycles: config.defaults.max_cycles,
    },
    workflow: {
      ...(config.workflow.max_cycles !== undefined
        ? { max_cycles: config.workflow.max_cycles }
        : {}),
      ...(config.workflow.max_actions !== undefined
        ? { max_actions: config.workflow.max_actions }
        : {}),
      ...(config.workflow.max_parallel !== undefined
        ? { max_parallel: config.workflow.max_parallel }
        : {}),
      ...(config.workflow.timeout_seconds !== undefined
        ? { timeout_seconds: config.workflow.timeout_seconds }
        : {}),
      nodes: config.workflow.nodes.map((node) => ({
        id: node.id,
        ...(node.agent_id ? { agent_id: node.agent_id } : {}),
        ...(node.skill ? { skill: node.skill } : {}),
        depends_on: [...node.depends_on],
        risk: node.risk,
        input: node.input,
        ...(node.beatdesign_card_id
          ? { beatdesign_card_id: node.beatdesign_card_id }
          : {}),
        ...(node.resources ? { resources: [...node.resources] } : {}),
        ...(node.timeout_seconds !== undefined
          ? { timeout_seconds: node.timeout_seconds }
          : {}),
      })),
    },
  };
}

/** Refuse credentials rather than silently dropping values and changing a workflow's meaning. */
export function credentialPaths(value: unknown, path = ""): string[] {
  if (
    typeof value === "string" &&
    /\b(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{16,})/i.test(value)
  )
    return [path];
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).flatMap(([key, item]) => {
    const next = path ? `${path}.${key}` : key;
    const sensitive =
      /(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|credential|cookie|private[_-]?key)/i.test(
        key,
      ) ||
      /(?:^|[_-])(?:api[_-]?key|access[_-]?key|token|password|passwd|secret|authorization|credentials?|private[_-]?key)(?:$|[_-])/i.test(
        key,
      ) ||
      /^(apiKey|accessKey|clientSecret|accessToken|refreshToken|privateKey)$/i.test(
        key,
      );
    return sensitive ? [next] : credentialPaths(item, next);
  });
}

export function workflowProblem(
  nodes: WorkflowNode[],
):
  | "empty"
  | "duplicate"
  | "cycle"
  | "dependency"
  | "agent"
  | "forbidden"
  | null {
  if (!nodes.length) return "empty";
  const ids = new Set(nodes.map((node) => node.id));
  if (ids.size !== nodes.length || nodes.some((node) => !node.id.trim()))
    return "duplicate";
  if (nodes.some((node) => node.depends_on.some((id) => !ids.has(id))))
    return "dependency";
  const visited = new Set<string>();
  const visiting = new Set<string>();
  const hasCycle = (id: string): boolean => {
    if (visiting.has(id)) return true;
    if (visited.has(id)) return false;
    visiting.add(id);
    if (nodes.find((node) => node.id === id)!.depends_on.some(hasCycle))
      return true;
    visiting.delete(id);
    visited.add(id);
    return false;
  };
  if (nodes.some((node) => hasCycle(node.id))) return "cycle";
  if (nodes.some((node) => node.risk === "R3")) return "forbidden";
  if (
    nodes.some((node) => node.skill !== "local.echo" && !node.agent_id?.trim())
  )
    return "agent";
  return null;
}

export function readLocalWorkspaceState(name: string): LocalWorkspaceState {
  const saved = localStorage.getItem(LOCAL_WORKSPACES_KEY);
  if (!saved) return createLocalWorkspaceState(name);
  const state = JSON.parse(saved) as LocalWorkspaceState;
  if (
    state.schema_version !== 1 ||
    typeof state.device_id !== "string" ||
    !Array.isArray(state.workspaces) ||
    !state.workspaces.length ||
    !state.workspaces.every(
      (workspace) =>
        typeof workspace.id === "string" && isWorkspaceConfig(workspace.config),
    ) ||
    !state.workspaces.some(
      (workspace) => workspace.id === state.active_workspace_id,
    )
  )
    throw new Error("INVALID_LOCAL_WORKSPACE");
  return state;
}
