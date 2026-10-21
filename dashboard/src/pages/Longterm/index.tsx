import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  CloudDownload,
  CloudUpload,
  GitBranch,
  HardDrive,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";
import { canAccessPath } from "../../utils/permissions";
import ComponentCenter from "./ComponentCenter";
import ComponentOperations, { ComponentArtifacts } from "./ComponentOperations";
import OperationSessionDialog, {
  type OperationSessionSeed,
} from "./OperationSessionDialog";
import EvaluationCenter from "./EvaluationCenter";
import { applySessionToDraft, SessionResultError } from "./sessionResults";
import { asRecord } from "./componentOperationsHelpers";
import type { ManagedComponentId } from "../../api/modules/componentManagement";
import PageShell from "../../layouts/PageShell";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import {
  formatServerDateTime,
  formatServerIsoDateTime,
} from "../../utils/formatMessageTime";
import { apiErrorMessage } from "../../utils/apiError";
import { message } from "../../utils/antdMessage";
import {
  longtermApi,
  type ComponentOperationDefinition,
  type IntegrationStatus,
  type LongtermTask,
  type TaskCommand,
  type TaskOperation,
  type TaskEvidence,
  type Tenant,
  type TenantMember,
  type TenantSpace,
  type WorkflowNode,
  type WorkflowDefinition,
  type WorkspaceConfig,
} from "../../api/modules/longterm";
import {
  createLocalWorkspaceState,
  credentialPaths,
  DEFAULT_COMPONENT_OPERATIONS,
  isWorkspaceConfig,
  localIdentifier,
  LOCAL_WORKSPACES_KEY,
  newLocalWorkspace,
  portableConfiguration,
  readLocalWorkspaceState,
  workflowProblem,
  type LocalWorkspace,
  type LocalWorkspaceState,
} from "./localWorkspace";
import styles from "./index.module.less";
import {
  BeatDesignImportError,
  importBeatDesignCanvas,
} from "./beatdesignImport";

const { Text, Paragraph } = Typography;
const terminalStates = new Set([
  "completed",
  "failed",
  "cancelled",
  "uncertain",
]);
const json = (value: unknown) => JSON.stringify(value, null, 2);
const statusColor = (status: string) =>
  ({
    completed: "success",
    running: "processing",
    waiting_approval: "warning",
    failed: "error",
    uncertain: "error",
    paused: "warning",
  })[status] ?? "default";

interface NodeForm {
  id: string;
  node_kind: "agent" | "local" | "component";
  agent_id?: string;
  skill?: string;
  component_id?: string;
  operation?: string;
  depends_on: string[];
  risk: WorkflowNode["risk"];
  input_json: string;
}

export default function LongtermPage({
  standalone = false,
  embedded = false,
  isVisible = true,
}: {
  standalone?: boolean;
  embedded?: boolean;
  isVisible?: boolean;
}) {
  const { t } = useTranslation();
  const user = useCurrentUser();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSection = searchParams.get("section");
  const activeSection = [
    "workflow",
    "sync",
    "tasks",
    "integrations",
    "evaluation",
  ].includes(requestedSection ?? "")
    ? requestedSection!
    : standalone
    ? "workflow"
    : "integrations";
  const navigateSection = (
    section: "workflow" | "sync" | "tasks" | "integrations" | "evaluation",
  ) => {
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("section", section);
      return next;
    });
  };
  const timezone = useServerTimezone(!standalone);
  const [bootstrap] = useState(() => {
    try {
      return {
        state: readLocalWorkspaceState(t("longterm.defaultWorkspace")),
        error: false,
      };
    } catch {
      return {
        state: createLocalWorkspaceState(t("longterm.defaultWorkspace")),
        error: true,
      };
    }
  });
  const [local, setLocal] = useState<LocalWorkspaceState>(bootstrap.state);
  const [storageBlocked, setStorageBlocked] = useState(bootstrap.error);
  const [storageError, setStorageError] = useState(
    bootstrap.error ? t("longterm.storageReadError") : "",
  );
  const [online, setOnline] = useState(navigator.onLine);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState("");
  const [spaces, setSpaces] = useState<TenantSpace[]>([]);
  const [members, setMembers] = useState<TenantMember[]>([]);
  const [membersTenant, setMembersTenant] = useState("");
  const [memberUserId, setMemberUserId] = useState<number | null>(null);
  const [memberRole, setMemberRole] = useState<"admin" | "member" | "viewer">(
    "member",
  );
  const [spaceId, setSpaceId] = useState("");
  const [tasks, setTasks] = useState<LongtermTask[]>([]);
  const [integrations, setIntegrations] = useState<IntegrationStatus[]>([]);
  const [componentOperations, setComponentOperations] = useState<
    ComponentOperationDefinition[]
  >(DEFAULT_COMPONENT_OPERATIONS);
  const [detail, setDetail] = useState<LongtermTask | null>(null);
  const [evidence, setEvidence] = useState<TaskEvidence | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [eventFilter, setEventFilter] = useState("all");
  const [nameDialog, setNameDialog] = useState<
    "local" | "tenant" | "space" | null
  >(null);
  const [newName, setNewName] = useState("");
  const [nodeDialog, setNodeDialog] = useState<{ index: number } | null>(null);
  const [nodeForm] = Form.useForm<NodeForm>();
  const nodeKind = Form.useWatch("node_kind", nodeForm);
  const nodeComponent = Form.useWatch("component_id", nodeForm);
  const nodeOperation = Form.useWatch("operation", nodeForm);
  const selectedComponentOperation = componentOperations.find(
    (item) =>
      item.component_id === nodeComponent && item.operation === nodeOperation,
  );
  const [goal, setGoal] = useState("");
  const [beatdesignOpen, setBeatdesignOpen] = useState(false);
  const [beatdesignInput, setBeatdesignInput] = useState("");
  const [beatdesignPreview, setBeatdesignPreview] =
    useState<WorkflowDefinition | null>(null);
  const [approval, setApproval] = useState<LongtermTask | null>(null);
  const [approvalChecked, setApprovalChecked] = useState(false);
  const [approvalNodeId, setApprovalNodeId] = useState("");
  const [operation, setOperation] = useState<{
    task: LongtermTask;
    kind: TaskOperation;
  } | null>(null);
  const [operationSession, setOperationSession] =
    useState<OperationSessionSeed | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const importRef = useRef<HTMLInputElement>(null);
  const beatdesignFileRef = useRef<HTMLInputElement>(null);
  const taskSubmission = useRef<{ payload: string; key: string } | null>(null);
  const scopeRef = useRef({ tenantId, spaceId });
  scopeRef.current = { tenantId, spaceId };
  const workspace = local.workspaces.find(
    (item) => item.id === local.active_workspace_id,
  )!;
  const config = workspace.config;
  const connected = !!user && online && isVisible;
  const targetReady = connected && !!tenantId && !!spaceId;
  const currentTenantRole = tenants.find(
    (tenant) => tenant.tenant_id === tenantId,
  )?.role;
  const permissionScope = JSON.stringify([
    user?.id,
    user?.role,
    currentTenantRole,
  ]);
  const sessionPermissionScope = useRef(permissionScope);
  const boundRevision =
    workspace.sync?.tenant_id === tenantId &&
    workspace.sync?.space_id === spaceId
      ? workspace.sync.revision
      : 0;

  useEffect(() => {
    setOperation(null);
    setOperationSession(null);
  }, [tenantId, spaceId]);

  useEffect(() => {
    // A session opened in the current scope must survive ordinary data refreshes.
    if (sessionPermissionScope.current !== permissionScope)
      setOperationSession(null);
  }, [permissionScope]);

  useEffect(() => {
    if (storageBlocked) return;
    try {
      localStorage.setItem(LOCAL_WORKSPACES_KEY, json(local));
      setStorageError("");
    } catch {
      setStorageError(t("longterm.storageWriteError"));
    }
  }, [local, storageBlocked, t]);

  useEffect(() => {
    const updateOnline = () => setOnline(navigator.onLine);
    const detectOtherTab = (event: StorageEvent) => {
      if (event.key === LOCAL_WORKSPACES_KEY) {
        setStorageBlocked(true);
        setStorageError(t("longterm.otherTabChanged"));
      }
    };
    window.addEventListener("online", updateOnline);
    window.addEventListener("offline", updateOnline);
    window.addEventListener("storage", detectOtherTab);
    return () => {
      window.removeEventListener("online", updateOnline);
      window.removeEventListener("offline", updateOnline);
      window.removeEventListener("storage", detectOtherTab);
    };
  }, [t]);

  const run = useCallback(
    async (action: () => Promise<void>) => {
      setBusy(true);
      setError("");
      try {
        await action();
      } catch (err) {
        setError(apiErrorMessage(err, t("longterm.requestFailed"), t));
      } finally {
        setBusy(false);
      }
    },
    [t],
  );

  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    void Promise.allSettled([
      longtermApi.tenants(),
      longtermApi.integrations(),
      longtermApi.componentOperations(),
    ]).then(([tenantResult, integrationResult, operationsResult]) => {
      if (cancelled) return;
      if (tenantResult.status === "fulfilled")
        setTenants(tenantResult.value.tenants);
      else
        setError(
          apiErrorMessage(tenantResult.reason, t("longterm.requestFailed"), t),
        );
      if (integrationResult.status === "fulfilled")
        setIntegrations(integrationResult.value.integrations);
      else
        setError(
          apiErrorMessage(
            integrationResult.reason,
            t("longterm.requestFailed"),
            t,
          ),
        );
      if (
        operationsResult.status === "fulfilled" &&
        operationsResult.value.operations.length
      )
        setComponentOperations(operationsResult.value.operations);
    });
    return () => {
      cancelled = true;
    };
  }, [connected, refreshKey, t]);

  useEffect(() => {
    if (!connected || !tenantId) return;
    let cancelled = false;
    void longtermApi
      .spaces(tenantId)
      .then((response) => {
        if (!cancelled) setSpaces(response.spaces);
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setError(apiErrorMessage(err, t("longterm.requestFailed"), t));
      });
    const refresh = () =>
      longtermApi
        .tasks(tenantId)
        .then((response) => {
          if (!cancelled) setTasks(response.tasks);
        })
        .catch((err: unknown) => {
          if (!cancelled)
            setError(apiErrorMessage(err, t("longterm.requestFailed"), t));
        });
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [connected, tenantId, refreshKey, t]);

  const detailId = detail?.task_id;
  useEffect(() => {
    if (!connected || !tenantId || !detailId) return;
    let cancelled = false;
    setEvidence(null);
    setDetailLoading(true);
    const refresh = async () => {
      try {
        const [task, proof] = await Promise.all([
          longtermApi.task(tenantId, detailId),
          longtermApi.evidence(tenantId, detailId),
        ]);
        if (!cancelled) {
          setDetail(task);
          setEvidence(proof);
        }
      } catch (err) {
        if (!cancelled)
          setError(apiErrorMessage(err, t("longterm.requestFailed"), t));
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    };
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [connected, detailId, tenantId, refreshKey, t]);

  function updateConfig(update: Partial<WorkspaceConfig>) {
    setLocal((previous) => ({
      ...previous,
      workspaces: previous.workspaces.map((item) =>
        item.id === previous.active_workspace_id
          ? {
              ...item,
              config: { ...item.config, ...update },
              updated_at: new Date().toISOString(),
            }
          : item,
      ),
    }));
  }

  function addWorkspace(item: LocalWorkspace) {
    setLocal((previous) => ({
      ...previous,
      active_workspace_id: item.id,
      workspaces: [...previous.workspaces, item],
    }));
  }

  function assertPortable() {
    const portable = portableConfiguration(config);
    const paths = credentialPaths(portable);
    if (paths.length)
      throw new Error(
        t("longterm.credentialsBlocked", { paths: paths.join(", ") }),
      );
    return portable;
  }

  async function createNamed() {
    const name = newName.trim();
    if (!name || !nameDialog) return;
    if (nameDialog === "local") {
      addWorkspace(newLocalWorkspace(name));
      setNameDialog(null);
      return;
    }
    await run(async () => {
      if (nameDialog === "tenant") {
        const tenant = await longtermApi.createTenant(name);
        setTenants((previous) => [...previous, tenant]);
        setTenantId(tenant.tenant_id);
        setSpaceId("");
        setSpaces([]);
        setTasks([]);
        setDetail(null);
      } else {
        const tenant = tenantId;
        const space = await longtermApi.createSpace(tenant, name);
        if (scopeRef.current.tenantId === tenant) {
          setSpaces((previous) => [...previous, space]);
          setSpaceId(space.space_id);
        }
      }
      setNameDialog(null);
    });
  }

  async function uploadConfig() {
    await run(async () => {
      const tenant = tenantId,
        space = spaceId,
        workspaceId = workspace.id;
      try {
        const response = await longtermApi.uploadConfiguration(tenant, space, {
          expected_revision: boundRevision,
          device_id: local.device_id,
          config: assertPortable(),
        });
        setLocal((previous) => ({
          ...previous,
          workspaces: previous.workspaces.map((item) =>
            item.id === workspaceId
              ? {
                  ...item,
                  sync: {
                    tenant_id: tenant,
                    space_id: space,
                    revision: response.revision,
                    synced_at: new Date().toISOString(),
                  },
                }
              : item,
          ),
        }));
        message.success(
          t("longterm.uploaded", { revision: response.revision }),
        );
        setRefreshKey((value) => value + 1);
      } catch (err) {
        if (err instanceof Error && /\b409\b/.test(err.message))
          throw new Error(t("longterm.syncConflict"));
        throw err;
      }
    });
  }

  async function downloadConfig() {
    await run(async () => {
      const tenant = tenantId,
        space = spaceId;
      const response = await longtermApi.configuration(tenant, space);
      if (!isWorkspaceConfig(response.config))
        throw new Error(t("longterm.noRemoteConfig"));
      const item = newLocalWorkspace(response.config.workspace_name);
      item.config = portableConfiguration(response.config);
      item.sync = {
        tenant_id: tenant,
        space_id: space,
        revision: response.revision,
        synced_at: new Date().toISOString(),
      };
      addWorkspace(item);
      message.success(t("longterm.downloaded"));
    });
  }

  function editNode(index: number) {
    const node = config.workflow.nodes[index];
    nodeForm.resetFields();
    nodeForm.setFieldsValue(
      node
        ? {
            ...node,
            node_kind:
              node.skill === "component.call"
                ? "component"
                : node.skill === "local.echo"
                ? "local"
                : "agent",
            agent_id: node.agent_id ?? "",
            input_json: json(node.input),
          }
        : {
            id: `step_${config.workflow.nodes.length + 1}_${Date.now().toString(
              36,
            )}`,
            agent_id: "",
            node_kind: "agent",
            skill: "",
            risk: "R0",
            depends_on: [],
            input_json: "{}",
          },
    );
    setNodeDialog({ index });
  }

  function selectComponentOperation(componentId: string, operation?: string) {
    const entry = componentOperations.find(
      (item) =>
        item.component_id === componentId &&
        (operation === undefined || item.operation === operation),
    );
    nodeForm.setFieldsValue({
      component_id: componentId,
      operation: entry?.operation,
      risk: entry?.risk ?? "R0",
      input_json: json(entry?.input_example ?? {}),
    });
  }

  async function saveNode() {
    const fields = await nodeForm.validateFields();
    if (!nodeDialog) return;
    const previous = config.workflow.nodes[nodeDialog.index];
    const skill =
      fields.node_kind === "component"
        ? "component.call"
        : fields.node_kind === "local"
        ? "local.echo"
        : fields.skill?.trim();
    const node: WorkflowNode = {
      id: fields.id.trim(),
      ...(fields.node_kind === "agent" && fields.agent_id?.trim()
        ? { agent_id: fields.agent_id.trim() }
        : {}),
      ...(skill ? { skill } : {}),
      ...(fields.node_kind === "component"
        ? { component_id: fields.component_id, operation: fields.operation }
        : {}),
      risk: fields.risk,
      depends_on: fields.depends_on,
      input: JSON.parse(fields.input_json) as Record<string, unknown>,
      ...(previous?.beatdesign_card_id
        ? { beatdesign_card_id: previous.beatdesign_card_id }
        : {}),
      ...(previous?.resources ? { resources: previous.resources } : {}),
      ...(previous?.timeout_seconds !== undefined
        ? { timeout_seconds: previous.timeout_seconds }
        : {}),
    };
    const nodes = [...config.workflow.nodes];
    nodes[nodeDialog.index] = node;
    const problem = workflowProblem(nodes, componentOperations);
    if (problem && !["agent", "forbidden"].includes(problem)) {
      message.error(t(`longterm.workflowErrors.${problem}`));
      return;
    }
    updateConfig({ workflow: { ...config.workflow, nodes } });
    setNodeDialog(null);
  }

  async function createTask() {
    await run(async () => {
      const portable = assertPortable();
      const problem = workflowProblem(
        portable.workflow.nodes,
        componentOperations,
      );
      if (problem) throw new Error(t(`longterm.workflowErrors.${problem}`));
      const tenant = tenantId;
      const body = {
        space_id: spaceId,
        goal: goal.trim(),
        workflow: portable.workflow,
        mode: portable.defaults.mode,
        max_cycles: portable.defaults.max_cycles,
      };
      const payload = json({ tenant, ...body });
      if (taskSubmission.current?.payload !== payload)
        taskSubmission.current = { payload, key: localIdentifier() };
      const task = await longtermApi.createTask(tenant, {
        ...body,
        idempotency_key: taskSubmission.current.key,
      });
      taskSubmission.current = null;
      if (scopeRef.current.tenantId === tenant) {
        setTasks((previous) => [
          task,
          ...previous.filter((item) => item.task_id !== task.task_id),
        ]);
        setDetail(task);
        setGoal("");
      }
      message.success(t("longterm.taskCreated"));
    });
  }

  async function commandTask(
    task: LongtermTask,
    command: TaskCommand,
    approvedNodeId?: string,
  ) {
    await run(async () => {
      const checkpoint = task.checkpoint ?? {};
      const planDigest = String(
        checkpoint.plan_digest ?? task.plan_digest ?? "",
      );
      const nodeId = approvedNodeId ?? String(checkpoint.node_id ?? "");
      if (command === "approve" && (!planDigest || !nodeId))
        throw new Error(t("longterm.approvalMissing"));
      const result = await longtermApi.command(task.tenant_id, task.task_id, {
        command,
        expected_revision: task.revision,
        ...(command === "approve"
          ? { plan_digest: planDigest, node_id: nodeId }
          : {}),
      });
      if (scopeRef.current.tenantId === task.tenant_id) {
        setTasks((previous) =>
          previous.map((item) =>
            item.task_id === result.task_id ? result : item,
          ),
        );
        setDetail((previous) =>
          previous?.task_id === result.task_id ? result : previous,
        );
      }
      setApproval(null);
      setRefreshKey((value) => value + 1);
      message.success(t("longterm.commandSent"));
    });
  }

  function taskActions(task: LongtermTask) {
    return (
      <Space wrap>
        {task.status === "draft" && (
          <Button
            size="small"
            disabled={busy || !connected}
            onClick={() => void commandTask(task, "start")}
          >
            {t("longterm.start")}
          </Button>
        )}
        {["running", "queued", "sleeping", "waiting_approval"].includes(
          task.status,
        ) && (
          <Button
            size="small"
            disabled={busy || !connected}
            onClick={() => void commandTask(task, "pause")}
          >
            {t("longterm.pause")}
          </Button>
        )}
        {["paused", "sleeping"].includes(task.status) && (
          <Button
            size="small"
            disabled={busy || !connected}
            onClick={() => void commandTask(task, "resume")}
          >
            {t("longterm.resume")}
          </Button>
        )}
        {task.status === "waiting_approval" && (
          <Button
            type="primary"
            size="small"
            disabled={busy || !connected}
            onClick={() => {
              setApproval(task);
              setApprovalChecked(false);
              setApprovalNodeId(
                String(
                  (Array.isArray(task.checkpoint?.waiting_nodes)
                    ? task.checkpoint.waiting_nodes[0]
                    : task.checkpoint?.node_id) ?? "",
                ),
              );
            }}
          >
            {t("longterm.review")}
          </Button>
        )}
        {!terminalStates.has(task.status) && (
          <Popconfirm
            title={t("longterm.cancelConfirm")}
            onConfirm={() => commandTask(task, "cancel")}
          >
            <Button size="small" danger disabled={busy || !connected}>
              {t("longterm.cancelTask")}
            </Button>
          </Popconfirm>
        )}
      </Space>
    );
  }

  function exportLocal() {
    const blob = new Blob(
      [json({ schema_version: 1, config: portableConfiguration(config) })],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "octop-workspace.json";
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function importLocal(file: File) {
    await run(async () => {
      const parsed = JSON.parse(await file.text()) as { config?: unknown };
      const imported = parsed.config ?? parsed;
      if (!isWorkspaceConfig(imported))
        throw new Error(t("longterm.invalidImport"));
      const item = newLocalWorkspace(imported.workspace_name);
      item.config = portableConfiguration(imported);
      addWorkspace(item);
      message.success(t("longterm.imported"));
    });
  }

  function openOperation(task: LongtermTask, kind: TaskOperation) {
    setOperation({ task, kind });
  }

  function openSession(seed: OperationSessionSeed) {
    if (!targetReady) {
      message.error(t("longterm.selectTargetFirst"));
      return;
    }
    sessionPermissionScope.current = permissionScope;
    setOperationSession(seed);
  }

  function returnSessionInput(
    input: Record<string, unknown>,
    targetId?: string,
  ) {
    if (storageBlocked || storageError) {
      message.error(t("longterm.storageWriteError"));
      return false;
    }
    try {
      const nodes = applySessionToDraft(config.workflow.nodes, input, targetId);
      updateConfig({ workflow: { ...config.workflow, nodes } });
      message.success(t("operationSessions.applied"));
      return true;
    } catch (cause) {
      message.error(
        cause instanceof SessionResultError
          ? t(`operationSessions.errors.${cause.key}`)
          : t("operationSessions.failed"),
      );
      return false;
    }
  }

  async function openMembers() {
    const tenant = tenantId;
    await run(async () => {
      const response = await longtermApi.members(tenant);
      if (scopeRef.current.tenantId === tenant) {
        setMembers(response.members);
        setMembersTenant(tenant);
      }
    });
  }

  async function previewBeatDesign(serverValidation: boolean) {
    setBeatdesignPreview(null);
    await run(async () => {
      let document: unknown;
      try {
        document = JSON.parse(beatdesignInput);
      } catch {
        throw new Error(t("longterm.jsonObjectRequired"));
      }
      let workflow: WorkflowDefinition;
      try {
        workflow = importBeatDesignCanvas(document);
      } catch (err) {
        if (err instanceof BeatDesignImportError)
          throw new Error(t(`longterm.beatdesignErrors.${err.code}`));
        throw err;
      }
      const paths = credentialPaths(workflow);
      if (serverValidation) paths.push(...credentialPaths(document));
      if (paths.length)
        throw new Error(
          t("longterm.credentialsBlocked", { paths: paths.join(", ") }),
        );
      if (serverValidation) {
        const response = await longtermApi.importBeatDesign(
          tenantId,
          document as Record<string, unknown>,
        );
        workflow = {
          ...response.workflow,
          nodes: response.workflow.nodes.map((node) => ({
            ...node,
            input: node.input ?? {},
          })),
        };
      }
      setBeatdesignPreview(workflow);
    });
  }

  function applyBeatDesign() {
    if (!beatdesignPreview) return;
    updateConfig({
      workflow: beatdesignPreview,
      defaults: {
        ...config.defaults,
        max_cycles: beatdesignPreview.max_cycles ?? config.defaults.max_cycles,
      },
    });
    setBeatdesignOpen(false);
    message.success(t("longterm.beatdesignApplied"));
  }

  async function changeMember(
    userId: number,
    role: "admin" | "member" | "viewer" | null,
  ) {
    await run(async () => {
      await longtermApi.setMember(membersTenant, userId, role);
      const response = await longtermApi.members(membersTenant);
      setMembers(response.members);
      setMemberUserId(null);
      message.success(t("longterm.membersUpdated"));
    });
  }

  const visibleTasks = tasks.filter(
    (task) => !spaceId || task.space_id === spaceId,
  );
  const events =
    evidence?.events.filter(
      (event) =>
        eventFilter === "all" ||
        event.event_type.toLowerCase().includes(eventFilter),
    ) ?? [];
  const content = (
    <div className={styles.content}>
      {storageError && (
        <Alert
          type="error"
          showIcon
          message={storageError}
          description={t("longterm.storageRecovery")}
          action={
            <Button onClick={() => window.location.reload()}>
              {t("longterm.reload")}
            </Button>
          }
        />
      )}
      {error && (
        <Alert
          type="error"
          showIcon
          closable
          message={error}
          onClose={() => setError("")}
        />
      )}
      <Alert
        type={connected ? "info" : "warning"}
        showIcon
        message={t(
          connected ? "longterm.localFirst" : "longterm.offlineNotice",
        )}
        description={t("longterm.credentialsNotice")}
      />
      <Card
        size="small"
        title={
          <Space>
            <HardDrive size={16} />
            {t("longterm.localWorkspace")}
          </Space>
        }
        extra={
          <Tag color={storageError ? "error" : "success"}>
            {t(storageError ? "longterm.unsaved" : "longterm.savedLocally")}
          </Tag>
        }
      >
        <div className={styles.toolbar}>
          <Select
            aria-label={t("longterm.localWorkspace")}
            className={styles.select}
            value={workspace.id}
            options={local.workspaces.map((item) => ({
              value: item.id,
              label: item.config.workspace_name,
            }))}
            onChange={(id) =>
              setLocal((previous) => ({ ...previous, active_workspace_id: id }))
            }
          />
          <Button
            icon={<Plus size={15} />}
            onClick={() => {
              setNameDialog("local");
              setNewName("");
            }}
          >
            {t("longterm.newLocal")}
          </Button>
          <Button onClick={exportLocal}>{t("longterm.export")}</Button>
          <Button onClick={() => importRef.current?.click()}>
            {t("longterm.import")}
          </Button>
          <input
            ref={importRef}
            type="file"
            accept="application/json,.json"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void importLocal(file);
              event.target.value = "";
            }}
          />
          <Text type="secondary">
            {t("longterm.updatedAt", {
              time: formatServerIsoDateTime(workspace.updated_at, timezone),
            })}
          </Text>
        </div>
      </Card>
      <Tabs
        activeKey={activeSection}
        onChange={(key) =>
          navigateSection(key as "workflow" | "sync" | "tasks" | "integrations")
        }
        items={[
          {
            key: "workflow",
            label: t("longterm.workflowTab"),
            children: (
              <div className={styles.content}>
                <Form layout="vertical">
                  <Form.Item label={t("longterm.workspaceName")}>
                    <Input
                      aria-label={t("longterm.workspaceName")}
                      maxLength={100}
                      value={config.workspace_name}
                      onChange={(event) =>
                        updateConfig({ workspace_name: event.target.value })
                      }
                    />
                  </Form.Item>
                  <div className={styles.grid}>
                    <Form.Item
                      label={t("longterm.executionMode")}
                      extra={t("longterm.localExecutionHint")}
                    >
                      <Select
                        value={config.defaults.mode}
                        onChange={(mode) =>
                          updateConfig({
                            defaults: { ...config.defaults, mode },
                          })
                        }
                        options={[
                          { value: "local", label: t("longterm.localRuntime") },
                          { value: "temporal", label: "Temporal" },
                        ]}
                      />
                    </Form.Item>
                    <Form.Item label={t("longterm.maxCycles")}>
                      <InputNumber
                        min={1}
                        max={32}
                        value={config.defaults.max_cycles}
                        onChange={(value) => {
                          if (value)
                            updateConfig({
                              defaults: {
                                ...config.defaults,
                                max_cycles: value,
                              },
                              workflow: {
                                ...config.workflow,
                                max_cycles: value,
                              },
                            });
                        }}
                      />
                    </Form.Item>
                  </div>
                </Form>
                <div className={styles.toolbar}>
                  <Text strong>
                    <GitBranch size={15} /> {t("longterm.workflowNodes")}
                  </Text>
                  <Button
                    icon={<Plus size={15} />}
                    onClick={() => editNode(config.workflow.nodes.length)}
                  >
                    {t("longterm.addNode")}
                  </Button>
                  <Button
                    onClick={() => {
                      setBeatdesignOpen(true);
                      setBeatdesignInput("");
                      setBeatdesignPreview(null);
                    }}
                  >
                    {t("longterm.beatdesignImport")}
                  </Button>
                  <Text type="secondary">{t("longterm.workflowHint")}</Text>
                </div>
                <Table
                  rowKey="id"
                  size="small"
                  dataSource={config.workflow.nodes}
                  pagination={false}
                  scroll={{ x: 720 }}
                  locale={{
                    emptyText: (
                      <Empty description={t("longterm.emptyWorkflow")} />
                    ),
                  }}
                  columns={[
                    { title: t("longterm.nodeId"), dataIndex: "id" },
                    {
                      title: t("longterm.agent"),
                      dataIndex: "agent_id",
                      render: (value: string) => value || "—",
                    },
                    {
                      title: t("longterm.skill"),
                      dataIndex: "skill",
                      render: (value: string, node: WorkflowNode) =>
                        value === "component.call"
                          ? `${node.component_id} / ${node.operation}`
                          : value || "—",
                    },
                    {
                      title: t("longterm.dependencies"),
                      dataIndex: "depends_on",
                      render: (value: string[]) => value.join(", ") || "—",
                    },
                    {
                      title: t("longterm.risk"),
                      dataIndex: "risk",
                      render: (value: string) => (
                        <Tag
                          color={
                            value === "R0"
                              ? "green"
                              : value === "R3"
                              ? "red"
                              : "orange"
                          }
                        >
                          {value}
                        </Tag>
                      ),
                    },
                    {
                      title: t("longterm.actions"),
                      render: (
                        _: unknown,
                        node: WorkflowNode,
                        index: number,
                      ) => (
                        <Space>
                          <Button
                            type="link"
                            size="small"
                            onClick={() => editNode(index)}
                          >
                            {t("longterm.edit")}
                          </Button>
                          {node.skill === "component.call" && (
                            <Button
                              type="link"
                              size="small"
                              disabled={!targetReady}
                              onClick={() =>
                                openSession({
                                  component_id:
                                    node.component_id as ManagedComponentId,
                                  operation:
                                    node.component_id === "semantica"
                                      ? "receipt"
                                      : node.operation,
                                  input:
                                    node.component_id === "semantica"
                                      ? {}
                                      : node.input,
                                })
                              }
                            >
                              {t("operationSessions.entry")}
                            </Button>
                          )}
                          <Popconfirm
                            title={t("longterm.deleteNodeConfirm")}
                            onConfirm={() =>
                              updateConfig({
                                workflow: {
                                  ...config.workflow,
                                  nodes: config.workflow.nodes
                                    .filter((item) => item.id !== node.id)
                                    .map((item) => ({
                                      ...item,
                                      depends_on: item.depends_on.filter(
                                        (id) => id !== node.id,
                                      ),
                                    })),
                                },
                              })
                            }
                          >
                            <Button
                              aria-label={t("longterm.deleteNode")}
                              type="text"
                              danger
                              icon={<Trash2 size={14} />}
                            />
                          </Popconfirm>
                        </Space>
                      ),
                    },
                  ]}
                />
              </div>
            ),
          },
          {
            key: "sync",
            label: t("longterm.syncTab"),
            children: (
              <div className={styles.content}>
                {!user && (
                  <Alert
                    type="info"
                    showIcon
                    message={t("longterm.loginToSync")}
                    action={<Link to="/login">{t("longterm.login")}</Link>}
                  />
                )}
                <div className={styles.grid}>
                  <Form.Item label={t("longterm.tenant")}>
                    <Space.Compact block>
                      <Select
                        className={styles.flexSelect}
                        aria-label={t("longterm.selectTenant")}
                        disabled={!connected || busy}
                        allowClear
                        placeholder={t("longterm.selectTenant")}
                        value={tenantId || undefined}
                        options={tenants.map((tenant) => ({
                          value: tenant.tenant_id,
                          label: `${tenant.name} (${tenant.role})`,
                        }))}
                        onChange={(id) => {
                          setTenantId(id ?? "");
                          setSpaceId("");
                          setSpaces([]);
                          setTasks([]);
                          setDetail(null);
                          setEvidence(null);
                        }}
                      />
                      <Button
                        disabled={!connected || busy}
                        onClick={() => {
                          setNameDialog("tenant");
                          setNewName("");
                        }}
                        icon={<Plus size={15} />}
                        aria-label={t("longterm.newTenant")}
                      />
                    </Space.Compact>
                    {["owner", "admin"].includes(
                      tenants.find((tenant) => tenant.tenant_id === tenantId)
                        ?.role ?? "",
                    ) && (
                      <Button
                        type="link"
                        disabled={!connected || busy}
                        onClick={() => void openMembers()}
                      >
                        {t("longterm.manageMembers")}
                      </Button>
                    )}
                  </Form.Item>
                  <Form.Item label={t("longterm.space")}>
                    <Space.Compact block>
                      <Select
                        className={styles.flexSelect}
                        aria-label={t("longterm.selectSpace")}
                        disabled={!connected || !tenantId || busy}
                        allowClear
                        value={spaceId || undefined}
                        placeholder={t("longterm.selectSpace")}
                        options={spaces.map((space) => ({
                          value: space.space_id,
                          label: space.name,
                        }))}
                        onChange={(id) => {
                          setSpaceId(id ?? "");
                          setDetail(null);
                          setEvidence(null);
                        }}
                      />
                      <Button
                        disabled={!connected || !tenantId || busy}
                        onClick={() => {
                          setNameDialog("space");
                          setNewName("");
                        }}
                        icon={<Plus size={15} />}
                        aria-label={t("longterm.newSpace")}
                      />
                    </Space.Compact>
                  </Form.Item>
                </div>
                <Descriptions
                  size="small"
                  column={1}
                  bordered
                  items={[
                    {
                      key: "device",
                      label: t("longterm.device"),
                      children: <Text copyable>{local.device_id}</Text>,
                    },
                    {
                      key: "revision",
                      label: t("longterm.baseRevision"),
                      children: boundRevision,
                    },
                    {
                      key: "sync",
                      label: t("longterm.lastSync"),
                      children: workspace.sync
                        ? formatServerIsoDateTime(
                            workspace.sync.synced_at,
                            timezone,
                          )
                        : t("longterm.neverSynced"),
                    },
                  ]}
                />
                <Alert type="info" showIcon message={t("longterm.syncHint")} />
                <Space wrap>
                  <Button
                    type="primary"
                    loading={busy}
                    disabled={!targetReady}
                    icon={<CloudUpload size={15} />}
                    onClick={() => void uploadConfig()}
                  >
                    {t("longterm.upload")}
                  </Button>
                  <Button
                    loading={busy}
                    disabled={!targetReady}
                    icon={<CloudDownload size={15} />}
                    onClick={() => void downloadConfig()}
                  >
                    {t("longterm.download")}
                  </Button>
                </Space>
              </div>
            ),
          },
          {
            key: "tasks",
            label: t("longterm.tasksTab"),
            children: (
              <div className={styles.content}>
                {!targetReady && (
                  <Alert
                    type="info"
                    showIcon
                    message={t("longterm.selectTargetFirst")}
                  />
                )}
                <Form layout="vertical">
                  <Form.Item label={t("longterm.taskGoal")}>
                    <Input.TextArea
                      rows={3}
                      maxLength={8000}
                      value={goal}
                      onChange={(event) => setGoal(event.target.value)}
                      placeholder={t("longterm.goalPlaceholder")}
                    />
                  </Form.Item>
                  <Button
                    type="primary"
                    loading={busy}
                    disabled={
                      !targetReady ||
                      !goal.trim() ||
                      !config.workflow.nodes.length
                    }
                    onClick={() => void createTask()}
                  >
                    {t("longterm.createTask")}
                  </Button>
                </Form>
                <Table
                  rowKey="task_id"
                  dataSource={visibleTasks}
                  size="small"
                  scroll={{ x: 780 }}
                  pagination={{ pageSize: 10 }}
                  locale={{ emptyText: t("longterm.noTasks") }}
                  columns={[
                    {
                      title: t("longterm.taskGoal"),
                      dataIndex: "goal",
                      ellipsis: true,
                      render: (value: string, task: LongtermTask) => (
                        <Button
                          type="link"
                          className={styles.goalLink}
                          onClick={() => {
                            setDetail(task);
                            setEventFilter("all");
                          }}
                        >
                          {value}
                        </Button>
                      ),
                    },
                    {
                      title: t("longterm.status"),
                      dataIndex: "status",
                      width: 150,
                      render: (value: string) => (
                        <Tag color={statusColor(value)}>
                          {t(`longterm.statuses.${value}`, {
                            defaultValue: value,
                          })}
                        </Tag>
                      ),
                    },
                    {
                      title: t("longterm.revision"),
                      dataIndex: "revision",
                      width: 80,
                    },
                    {
                      title: t("longterm.actions"),
                      width: 300,
                      render: (_: unknown, task: LongtermTask) =>
                        taskActions(task),
                    },
                  ]}
                />
              </div>
            ),
          },
          {
            key: "evaluation",
            label: t("evaluationCenter.title"),
            children: (
              <EvaluationCenter
                key={`${permissionScope}:${tenantId}:${spaceId}`}
                tenantId={tenantId}
                spaceId={spaceId}
                role={currentTenantRole}
                connected={connected && activeSection === "evaluation"}
                tasks={tasks}
                onOpenTask={(taskId) => {
                  const scope = { tenantId, spaceId };
                  void run(async () => {
                    const task = await longtermApi.task(scope.tenantId, taskId);
                    if (
                      scopeRef.current.tenantId === scope.tenantId &&
                      scopeRef.current.spaceId === scope.spaceId &&
                      task.space_id === scope.spaceId
                    )
                      setDetail(task);
                  });
                }}
                onSession={openSession}
              />
            ),
          },
          {
            key: "integrations",
            label: t("longterm.integrationsTab"),
            children: (
              <ComponentCenter
                connected={connected}
                isAdmin={user?.role === "admin"}
                actorId={user?.id}
                tenantId={tenantId}
                spaceId={spaceId}
                tenantRole={
                  tenants.find((tenant) => tenant.tenant_id === tenantId)?.role
                }
                tasks={spaceId ? visibleTasks : []}
                canAccessPath={(path) => canAccessPath(user, path)}
                onNavigate={navigateSection}
                onOperation={(kind, task) => openOperation(task, kind)}
                onSession={openSession}
                onChanged={() => setRefreshKey((value) => value + 1)}
              />
            ),
          },
        ]}
      />
    </div>
  );

  const headerActions = (
    <Space wrap>
      <Button
        disabled={!targetReady}
        onClick={() => openSession({ component_id: "octop" })}
      >
        {t("operationSessions.recent")}
      </Button>
      {standalone ? (
        <Link to="/longterm">{t("longterm.serverConsole")}</Link>
      ) : (
        <Link to="/offline-workbench">{t("longterm.offlineEntry")}</Link>
      )}
      <Button
        icon={<RefreshCw size={15} />}
        disabled={!connected || busy}
        onClick={() => setRefreshKey((value) => value + 1)}
      >
        {t("longterm.refresh")}
      </Button>
    </Space>
  );
  return (
    <>
      {standalone ? (
        <main className={styles.standalone}>
          <div className={styles.standaloneHeader}>
            <div>
              <Typography.Title level={3}>
                {t("longterm.title")}
              </Typography.Title>
              <Text type="secondary">{t("longterm.subtitle")}</Text>
            </div>
            {headerActions}
          </div>
          {content}
        </main>
      ) : embedded ? (
        <section className={styles.embedded}>
          <div className={styles.embeddedActions}>{headerActions}</div>
          {content}
        </section>
      ) : (
        <PageShell
          title={t("longterm.title")}
          subtitle={t("longterm.subtitle")}
          actions={headerActions}
        >
          {content}
        </PageShell>
      )}
      <Modal
        title={t(`longterm.newNames.${nameDialog ?? "local"}`)}
        open={isVisible && !!nameDialog}
        onCancel={() => setNameDialog(null)}
        onOk={() => void createNamed()}
        confirmLoading={busy}
        okButtonProps={{ disabled: !newName.trim() }}
      >
        <Input
          autoFocus
          aria-label={t("longterm.name")}
          maxLength={100}
          value={newName}
          onChange={(event) => setNewName(event.target.value)}
          onPressEnter={() => void createNamed()}
        />
      </Modal>
      <Modal
        title={t("longterm.editNode")}
        open={isVisible && !!nodeDialog}
        onCancel={() => setNodeDialog(null)}
        onOk={() => void saveNode().catch(() => undefined)}
        width={680}
        forceRender
      >
        <Form form={nodeForm} layout="vertical">
          <Form.Item
            name="id"
            label={t("longterm.nodeId")}
            rules={[
              { required: true },
              {
                pattern: /^[a-zA-Z0-9_-]+$/,
                message: t("longterm.nodeIdHint"),
              },
            ]}
          >
            <Input
              disabled={
                !!nodeDialog && nodeDialog.index < config.workflow.nodes.length
              }
            />
          </Form.Item>
          <Form.Item
            name="node_kind"
            label={t("longterm.nodeKind")}
            rules={[{ required: true }]}
          >
            <Select
              aria-label={t("longterm.nodeKind")}
              options={["agent", "local", "component"].map((kind) => ({
                value: kind,
                label: t(`longterm.nodeKinds.${kind}`),
              }))}
              onChange={(kind: NodeForm["node_kind"]) => {
                nodeForm.setFieldsValue({
                  skill: kind === "local" ? "local.echo" : "",
                  risk: "R0",
                });
                if (kind === "component")
                  selectComponentOperation(componentOperations[0].component_id);
              }}
            />
          </Form.Item>
          {nodeKind === "agent" && (
            <div className={styles.grid}>
              <Form.Item
                name="agent_id"
                label={t("longterm.agent")}
                dependencies={["skill"]}
                rules={[
                  ({ getFieldValue }) => ({
                    validator: (_, value: string | undefined) =>
                      getFieldValue("skill")?.trim() === "local.echo" ||
                      value?.trim()
                        ? Promise.resolve()
                        : Promise.reject(
                            new Error(t("longterm.agentRequired")),
                          ),
                  }),
                ]}
                extra={t("longterm.agentHint")}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="skill"
                label={t("longterm.skill")}
                extra={t("longterm.skillHint")}
                rules={[
                  {
                    validator: async (_, value?: string) => {
                      if (value?.trim() === "component.call")
                        throw new Error(t("longterm.componentSkillHint"));
                    },
                  },
                ]}
              >
                <Input />
              </Form.Item>
            </div>
          )}
          {nodeKind === "component" && (
            <>
              <div className={styles.grid}>
                <Form.Item
                  name="component_id"
                  label={t("longterm.nodeComponent")}
                  rules={[{ required: true }]}
                >
                  <Select
                    aria-label={t("longterm.nodeComponent")}
                    options={Array.from(
                      new Set(
                        componentOperations.map((item) => item.component_id),
                      ),
                    ).map((id) => ({
                      value: id,
                      label:
                        {
                          beatdesign: "BeatDesign",
                          rna: "RNA",
                          hindsight: "Hindsight",
                          commerceagentbench: "CommerceAgentBench",
                          semantica: "Semantica",
                        }[id] ?? id,
                    }))}
                    onChange={(id: string) => selectComponentOperation(id)}
                  />
                </Form.Item>
                <Form.Item
                  name="operation"
                  label={t("longterm.nodeOperation")}
                  rules={[{ required: true }]}
                >
                  <Select
                    aria-label={t("longterm.nodeOperation")}
                    options={componentOperations
                      .filter((item) => item.component_id === nodeComponent)
                      .map((item) => ({
                        value: item.operation,
                        label: item.operation,
                      }))}
                    onChange={(operation: string) => {
                      if (nodeComponent)
                        selectComponentOperation(nodeComponent, operation);
                    }}
                  />
                </Form.Item>
              </div>
              <Paragraph type="secondary">
                {t("longterm.componentNodeHint")}
                {selectedComponentOperation?.project_required &&
                  ` ${t("longterm.componentProjectRequired")}`}
              </Paragraph>
            </>
          )}
          <Form.Item name="depends_on" label={t("longterm.dependencies")}>
            <Select
              mode="multiple"
              options={config.workflow.nodes
                .filter((_, index) => index !== nodeDialog?.index)
                .map((node) => ({ value: node.id, label: node.id }))}
            />
          </Form.Item>
          <Form.Item
            name="risk"
            label={t("longterm.risk")}
            extra={t("longterm.riskHint")}
          >
            <Select
              disabled={nodeKind === "component"}
              options={["R0", "R1", "R2", "R3"].map((risk) => ({
                value: risk,
                label: t(`longterm.risks.${risk}`),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="input_json"
            label={t("longterm.inputJson")}
            extra={t("longterm.dependencyInputHint")}
            rules={[
              {
                validator: async (_, value: string) => {
                  try {
                    const parsed: unknown = JSON.parse(value);
                    if (
                      !parsed ||
                      typeof parsed !== "object" ||
                      Array.isArray(parsed)
                    )
                      throw new Error();
                  } catch {
                    throw new Error(t("longterm.jsonObjectRequired"));
                  }
                },
              },
            ]}
          >
            <Input.TextArea rows={7} className={styles.codeInput} />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={t("longterm.beatdesignImport")}
        open={isVisible && beatdesignOpen}
        onCancel={() => setBeatdesignOpen(false)}
        onOk={applyBeatDesign}
        okText={t("longterm.beatdesignApply")}
        okButtonProps={{ disabled: !beatdesignPreview || busy }}
        width={820}
      >
        <div className={styles.content}>
          <Alert type="info" showIcon message={t("longterm.beatdesignHint")} />
          <Form layout="vertical">
            <Form.Item label={t("longterm.beatdesignDocument")}>
              <Input.TextArea
                aria-label={t("longterm.beatdesignDocument")}
                rows={9}
                className={styles.codeInput}
                value={beatdesignInput}
                onChange={(event) => {
                  setBeatdesignInput(event.target.value);
                  setBeatdesignPreview(null);
                }}
              />
            </Form.Item>
          </Form>
          <Space wrap>
            <Button onClick={() => beatdesignFileRef.current?.click()}>
              {t("longterm.beatdesignFile")}
            </Button>
            <input
              ref={beatdesignFileRef}
              type="file"
              accept="application/json,.json"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file)
                  void run(async () => {
                    setBeatdesignInput(await file.text());
                    setBeatdesignPreview(null);
                  });
                event.target.value = "";
              }}
            />
            <Button
              loading={busy}
              disabled={!beatdesignInput.trim()}
              onClick={() => void previewBeatDesign(false)}
            >
              {t("longterm.beatdesignLocalPreview")}
            </Button>
            <Button
              loading={busy}
              disabled={!beatdesignInput.trim() || !connected || !tenantId}
              onClick={() => void previewBeatDesign(true)}
            >
              {t("longterm.beatdesignServerPreview")}
            </Button>
          </Space>
          {beatdesignPreview && (
            <>
              <Text strong>
                {t("longterm.beatdesignPreview", {
                  count: beatdesignPreview.nodes.length,
                })}
              </Text>
              <pre className={styles.json}>{json(beatdesignPreview)}</pre>
            </>
          )}
        </div>
      </Modal>
      <Drawer
        title={t("longterm.taskDetail")}
        open={isVisible && !!detail}
        onClose={() => {
          setDetail(null);
          setEvidence(null);
        }}
        width="min(900px, 100vw)"
      >
        {detail && (
          <div className={styles.content}>
            <Paragraph strong>{detail.goal}</Paragraph>
            <Descriptions
              bordered
              column={1}
              size="small"
              items={[
                {
                  key: "id",
                  label: t("longterm.taskId"),
                  children: <Text copyable>{detail.task_id}</Text>,
                },
                {
                  key: "status",
                  label: t("longterm.status"),
                  children: (
                    <Tag color={statusColor(detail.status)}>
                      {t(`longterm.statuses.${detail.status}`, {
                        defaultValue: detail.status,
                      })}
                    </Tag>
                  ),
                },
                {
                  key: "mode",
                  label: t("longterm.executionMode"),
                  children: detail.mode ?? "—",
                },
                {
                  key: "phase",
                  label: t("longterm.loopPhase"),
                  children: String(detail.checkpoint?.phase ?? "—"),
                },
                {
                  key: "cycle",
                  label: t("longterm.cycle"),
                  children: String(detail.checkpoint?.cycle ?? "—"),
                },
                {
                  key: "health",
                  label: t("longterm.executionHealth"),
                  children: String(detail.checkpoint?.execution_health ?? "—"),
                },
                {
                  key: "outcome",
                  label: t("longterm.goalOutcome"),
                  children: String(detail.checkpoint?.goal_outcome ?? "—"),
                },
              ]}
            />
            {taskActions(detail)}
            <Space wrap>
              <Button
                disabled={!targetReady}
                onClick={() =>
                  openSession({
                    component_id: "octop",
                    operation: "task",
                    task_id: detail.task_id,
                  })
                }
              >
                {t("operationSessions.entry")}
              </Button>
              {(["memory", "evaluate", "evolution", "provenance"] as const).map(
                (kind) => (
                  <Button
                    key={kind}
                    disabled={busy || !connected}
                    onClick={() => openOperation(detail, kind)}
                  >
                    {t(`longterm.operationNames.${kind}`)}
                  </Button>
                ),
              )}
            </Space>
            {detail.status === "uncertain" && (
              <Alert
                showIcon
                type="error"
                message={t("longterm.uncertainHint")}
              />
            )}
            <Tabs
              items={[
                {
                  key: "nodeResults",
                  label: t("operationSessions.nodeResults"),
                  children: (
                    <Table
                      rowKey="id"
                      size="small"
                      pagination={false}
                      dataSource={detail.workflow.nodes}
                      columns={[
                        { title: t("longterm.nodeId"), dataIndex: "id" },
                        { title: t("longterm.skill"), dataIndex: "skill" },
                        {
                          title: t("longterm.status"),
                          render: (_, node) =>
                            String(
                              asRecord(
                                asRecord(detail.checkpoint?.node_results)[
                                  node.id
                                ],
                              ).status ?? "—",
                            ),
                        },
                        {
                          title: t("longterm.actions"),
                          render: (_, node) => (
                            <Space wrap>
                              <Button
                                size="small"
                                disabled={!targetReady}
                                onClick={() =>
                                  openSession({
                                    component_id:
                                      node.skill === "component.call"
                                        ? (node.component_id as ManagedComponentId)
                                        : "octop",
                                    operation:
                                      node.skill === "component.call"
                                        ? node.component_id === "semantica"
                                          ? "receipt"
                                          : node.operation
                                        : "task",
                                    input:
                                      node.skill === "component.call" &&
                                      node.component_id !== "semantica"
                                        ? node.input
                                        : {},
                                    task_id: detail.task_id,
                                    node_id: node.id,
                                  })
                                }
                              >
                                {t("operationSessions.entry")}
                              </Button>
                              <Button
                                size="small"
                                disabled={
                                  !targetReady ||
                                  asRecord(
                                    asRecord(detail.checkpoint?.node_results)[
                                      node.id
                                    ],
                                  ).status !== "succeeded"
                                }
                                onClick={() =>
                                  openSession({
                                    component_id: "octop",
                                    operation: "node_result",
                                    task_id: detail.task_id,
                                    node_id: node.id,
                                  })
                                }
                              >
                                {t("operationSessions.returnNodeResult")}
                              </Button>
                            </Space>
                          ),
                        },
                      ]}
                      expandable={{
                        expandedRowRender: (node) => (
                          <pre className={styles.json}>
                            {json(
                              asRecord(detail.checkpoint?.node_results)[
                                node.id
                              ] ?? null,
                            )}
                          </pre>
                        ),
                      }}
                    />
                  ),
                },
                {
                  key: "evidence",
                  label: t("longterm.evidence"),
                  children: (
                    <div className={styles.content}>
                      {evidence && (
                        <Alert
                          type={evidence.valid ? "success" : "error"}
                          showIcon
                          message={t(
                            evidence.valid
                              ? "longterm.evidenceValid"
                              : "longterm.evidenceInvalid",
                          )}
                          description={
                            <Text code copyable className={styles.digest}>
                              {evidence.head_digest}
                            </Text>
                          }
                        />
                      )}
                      <Select
                        aria-label={t("longterm.eventFilter")}
                        value={eventFilter}
                        onChange={setEventFilter}
                        options={[
                          "all",
                          "memory",
                          "evaluat",
                          "evol",
                          "approval",
                        ].map((value) => ({
                          value,
                          label: t(`longterm.eventFilters.${value}`),
                        }))}
                      />
                      <Table
                        rowKey="sequence"
                        size="small"
                        loading={detailLoading}
                        dataSource={events}
                        pagination={{ pageSize: 8 }}
                        expandable={{
                          expandedRowRender: (event) => (
                            <pre className={styles.json}>{json(event)}</pre>
                          ),
                        }}
                        scroll={{ x: 620 }}
                        columns={[
                          { title: "#", dataIndex: "sequence", width: 60 },
                          {
                            title: t("longterm.event"),
                            dataIndex: "event_type",
                          },
                          {
                            title: t("longterm.time"),
                            dataIndex: "created_at",
                            render: (value: string | number) =>
                              typeof value === "number"
                                ? formatServerDateTime(value, timezone)
                                : formatServerIsoDateTime(value, timezone),
                          },
                        ]}
                        locale={{ emptyText: t("longterm.noEvidence") }}
                      />
                    </div>
                  ),
                },
                {
                  key: "checkpoint",
                  label: t("longterm.checkpoint"),
                  children: (
                    <pre className={styles.json}>
                      {json(detail.checkpoint ?? {})}
                    </pre>
                  ),
                },
                {
                  key: "artifacts",
                  label: t("componentOperations.artifacts"),
                  children: <ComponentArtifacts task={detail} />,
                },
                {
                  key: "result",
                  label: t("longterm.result"),
                  children: (
                    <pre className={styles.json}>
                      {json(detail.result ?? null)}
                    </pre>
                  ),
                },
                {
                  key: "workflow",
                  label: t("longterm.workflowTab"),
                  children: (
                    <pre className={styles.json}>{json(detail.workflow)}</pre>
                  ),
                },
              ]}
            />
          </div>
        )}
      </Drawer>
      <Modal
        title={t("longterm.approvalTitle")}
        open={isVisible && !!approval}
        onCancel={() => setApproval(null)}
        onOk={() => {
          if (approval) void commandTask(approval, "approve", approvalNodeId);
        }}
        okText={t("longterm.approve")}
        okButtonProps={{ disabled: !approvalChecked || !approvalNodeId }}
        confirmLoading={busy}
        width={720}
      >
        {approval && (
          <div className={styles.content}>
            <Alert
              type="warning"
              showIcon
              message={t("longterm.approvalHint")}
            />
            <Descriptions
              column={1}
              bordered
              size="small"
              items={[
                {
                  key: "goal",
                  label: t("longterm.taskGoal"),
                  children: approval.goal,
                },
                {
                  key: "node",
                  label: t("longterm.nodeId"),
                  children: (
                    <Select
                      aria-label={t("longterm.nodeId")}
                      value={approvalNodeId || undefined}
                      options={(Array.isArray(
                        approval.checkpoint?.waiting_nodes,
                      )
                        ? approval.checkpoint.waiting_nodes
                        : [approval.checkpoint?.node_id]
                      )
                        .filter((id): id is string => typeof id === "string")
                        .map((id) => ({ value: id, label: id }))}
                      onChange={(id) => {
                        setApprovalNodeId(id);
                        setApprovalChecked(false);
                      }}
                    />
                  ),
                },
                {
                  key: "digest",
                  label: t("longterm.planDigest"),
                  children: (
                    <Text code copyable className={styles.digest}>
                      {String(
                        approval.checkpoint?.plan_digest ??
                          approval.plan_digest ??
                          "—",
                      )}
                    </Text>
                  ),
                },
                {
                  key: "revision",
                  label: t("longterm.revision"),
                  children: approval.revision,
                },
              ]}
            />
            <pre className={styles.json}>
              {json(
                approval.workflow.nodes.find(
                  (node) => node.id === approvalNodeId,
                ) ?? approval.checkpoint,
              )}
            </pre>
            <Checkbox
              checked={approvalChecked}
              onChange={(event) => setApprovalChecked(event.target.checked)}
            >
              {t("longterm.approvalConfirm")}
            </Checkbox>
          </div>
        )}
      </Modal>
      <Modal
        title={t("longterm.manageMembers")}
        open={isVisible && !!membersTenant}
        onCancel={() => setMembersTenant("")}
        footer={null}
        width={680}
      >
        <div className={styles.content}>
          <Alert type="info" showIcon message={t("longterm.membersHint")} />
          <Space wrap>
            <InputNumber
              aria-label={t("longterm.userId")}
              placeholder={t("longterm.userId")}
              min={1}
              precision={0}
              value={memberUserId}
              onChange={setMemberUserId}
            />
            <Select
              aria-label={t("longterm.memberRole")}
              value={memberRole}
              onChange={setMemberRole}
              options={(["admin", "member", "viewer"] as const).map((role) => ({
                value: role,
                label: t(`longterm.memberRoles.${role}`),
              }))}
            />
            <Button
              type="primary"
              disabled={!memberUserId || busy}
              onClick={() => {
                if (memberUserId) void changeMember(memberUserId, memberRole);
              }}
            >
              {t("longterm.assignMember")}
            </Button>
          </Space>
          <Table
            rowKey="user_id"
            dataSource={members}
            pagination={false}
            columns={[
              { title: t("longterm.userId"), dataIndex: "user_id" },
              {
                title: t("longterm.memberRole"),
                dataIndex: "role",
                render: (role: string) => t(`longterm.memberRoles.${role}`),
              },
              {
                title: t("longterm.actions"),
                render: (_: unknown, member: TenantMember) =>
                  member.role !== "owner" && (
                    <Popconfirm
                      title={t("longterm.removeMemberConfirm")}
                      onConfirm={() => changeMember(member.user_id, null)}
                    >
                      <Button danger size="small" disabled={busy}>
                        {t("longterm.removeMember")}
                      </Button>
                    </Popconfirm>
                  ),
              },
            ]}
          />
        </div>
      </Modal>
      {operation &&
        connected &&
        operation.task.tenant_id === tenantId &&
        operation.task.space_id === spaceId && (
          <ComponentOperations
            key={`${operation.task.task_id}:${operation.kind}`}
            task={
              detail?.task_id === operation.task.task_id
                ? detail
                : tasks.find(
                    (task) => task.task_id === operation.task.task_id,
                  ) ?? operation.task
            }
            kind={operation.kind}
            role={tenants.find((tenant) => tenant.tenant_id === tenantId)?.role}
            integration={integrations.find(
              (item) =>
                item.id ===
                {
                  memory: "hindsight",
                  evaluate: "commerceagentbench",
                  evolution: "rna",
                  provenance: "semantica",
                }[operation.kind],
            )}
            onCancel={() => setOperation(null)}
            onSuccess={() => setRefreshKey((value) => value + 1)}
            onReturnResult={(result, input) => {
              const componentId = (
                {
                  memory: "hindsight",
                  evaluate: "commerceagentbench",
                  evolution: "rna",
                  provenance: "semantica",
                } as const
              )[operation.kind];
              const seed: OperationSessionSeed = {
                component_id: componentId,
                task_id: operation.task.task_id,
                initial_result: result,
                ...(input
                  ? { input }
                  : componentId === "hindsight"
                  ? { input: { query: operation.task.goal, max_tokens: 2048 } }
                  : {}),
              };
              setOperation(null);
              openSession(seed);
            }}
            onManageComponent={() => {
              setOperation(null);
              setDetail(null);
              navigateSection("integrations");
            }}
          />
        )}
      {operationSession &&
        targetReady &&
        sessionPermissionScope.current === permissionScope && (
          <OperationSessionDialog
            key={`${permissionScope}:${tenantId}:${spaceId}:${
              workspace.id
            }:${json(operationSession)}`}
            tenantId={tenantId}
            spaceId={spaceId}
            seed={operationSession}
            tasks={tasks}
            draftNodes={config.workflow.nodes}
            connected={connected && isVisible}
            onClose={() => setOperationSession(null)}
            onApply={returnSessionInput}
          />
        )}
    </>
  );
}
