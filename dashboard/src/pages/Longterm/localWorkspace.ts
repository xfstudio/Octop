import type {
  ComponentOperationDefinition,
  WorkspaceConfig,
  WorkflowNode,
} from "../../api/modules/longterm";

export const LOCAL_WORKSPACES_KEY = "octop:longterm:workspaces:v1";

/** Bundled operation metadata keeps workflow editing available without a server. */
export const DEFAULT_COMPONENT_OPERATIONS: ComponentOperationDefinition[] = [
  ...[
    "project",
    "canvas",
    "assets",
    "skills",
    "generation_models",
    "generation_history",
    "editor",
    "editor_diagnostics",
  ].map((operation) => ({
    component_id: "beatdesign",
    operation,
    risk: "R0" as const,
    input_example: {},
    project_required: true,
  })),
  ...["genes", "observability"].map((operation) => ({
    component_id: "rna",
    operation,
    risk: "R0" as const,
    input_example: {},
    project_required: true,
  })),
  {
    component_id: "hindsight",
    operation: "recall",
    risk: "R0",
    input_example: { query: "", max_tokens: 4096 },
    project_required: false,
  },
  {
    component_id: "commerceagentbench",
    operation: "report",
    risk: "R0",
    input_example: { run_dir: "" },
    project_required: false,
  },
  {
    component_id: "semantica",
    operation: "index",
    risk: "R1",
    input_example: { consent: true },
    project_required: false,
  },
];

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
        (node.skill === undefined || typeof node.skill === "string") &&
        (node.agent_id === undefined || typeof node.agent_id === "string") &&
        (node.component_id === undefined ||
          typeof node.component_id === "string") &&
        (node.operation === undefined || typeof node.operation === "string") &&
        (node.skill !== "component.call" ||
          (!!node.component_id?.trim() && !!node.operation?.trim())) &&
        (node.skill === "component.call" ||
          (node.component_id === undefined && node.operation === undefined)) &&
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
        ...(node.skill === "component.call" && node.component_id
          ? { component_id: node.component_id }
          : {}),
        ...(node.skill === "component.call" && node.operation
          ? { operation: node.operation }
          : {}),
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
  operations: ComponentOperationDefinition[] = DEFAULT_COMPONENT_OPERATIONS,
):
  | "empty"
  | "duplicate"
  | "cycle"
  | "dependency"
  | "agent"
  | "component"
  | "reference"
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
    nodes.some((node) => {
      if (node.skill !== "component.call")
        return node.component_id !== undefined || node.operation !== undefined;
      const operation = operations.find(
        (item) =>
          item.component_id === node.component_id &&
          item.operation === node.operation,
      );
      return (
        !operation ||
        operation.risk !== node.risk ||
        (node.component_id === "semantica" &&
          node.operation === "index" &&
          node.input.consent !== true)
      );
    })
  )
    return "component";
  const validReferences = (value: unknown, dependencies: string[]): boolean => {
    if (!value || typeof value !== "object") return true;
    if (Array.isArray(value))
      return value.every((item) => validReferences(item, dependencies));
    const record = value as Record<string, unknown>;
    if ("$node" in record)
      return (
        typeof record.$node === "string" &&
        dependencies.includes(record.$node) &&
        Object.keys(record).every((key) =>
          ["$node", "pointer"].includes(key),
        ) &&
        (record.pointer === undefined ||
          (typeof record.pointer === "string" &&
            (record.pointer === "" || record.pointer.startsWith("/")) &&
            !/~(?:[^01]|$)/.test(record.pointer)))
      );
    return Object.values(record).every((item) =>
      validReferences(item, dependencies),
    );
  };
  if (nodes.some((node) => !validReferences(node.input, node.depends_on)))
    return "reference";
  if (
    nodes.some(
      (node) =>
        !["local.echo", "component.call"].includes(node.skill ?? "") &&
        !node.agent_id?.trim(),
    )
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
