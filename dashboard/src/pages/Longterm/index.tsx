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
import { Link } from "react-router-dom";
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
  agent_id: string;
  skill?: string;
  depends_on: string[];
  risk: WorkflowNode["risk"];
  input_json: string;
}

export default function LongtermPage({
  standalone = false,
}: {
  standalone?: boolean;
}) {
  const { t } = useTranslation();
  const user = useCurrentUser();
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
  const [operationInput, setOperationInput] = useState("");
  const [operationResult, setOperationResult] = useState<unknown>(null);
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
  const connected = !!user && online;
  const targetReady = connected && !!tenantId && !!spaceId;
  const boundRevision =
    workspace.sync?.tenant_id === tenantId &&
    workspace.sync?.space_id === spaceId
      ? workspace.sync.revision
      : 0;

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
    ]).then(([tenantResult, integrationResult]) => {
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
    nodeForm.setFieldsValue(
      node
        ? {
            ...node,
            agent_id: node.agent_id ?? "",
            input_json: json(node.input),
          }
        : {
            id: `step_${config.workflow.nodes.length + 1}_${Date.now().toString(
              36,
            )}`,
            agent_id: "",
            skill: "",
            risk: "R0",
            depends_on: [],
            input_json: "{}",
          },
    );
    setNodeDialog({ index });
  }

  async function saveNode() {
    const fields = await nodeForm.validateFields();
    if (!nodeDialog) return;
    const node: WorkflowNode = {
      ...config.workflow.nodes[nodeDialog.index],
      id: fields.id.trim(),
      agent_id: fields.agent_id.trim(),
      ...(fields.skill?.trim() ? { skill: fields.skill.trim() } : {}),
      risk: fields.risk,
      depends_on: fields.depends_on,
      input: JSON.parse(fields.input_json) as Record<string, unknown>,
    };
    const nodes = [...config.workflow.nodes];
    nodes[nodeDialog.index] = node;
    const problem = workflowProblem(nodes);
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
      const problem = workflowProblem(portable.workflow.nodes);
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
    const body = {
      evaluate: { run_dir: "" },
      evolution: {
        action: "propose",
        proposal: { summary: "", hypothesis: "" },
        benchmark_revision: "",
      },
      memory: { action: "recall", query: "", max_tokens: 2048 },
      provenance: { consent: false },
    }[kind];
    setOperation({ task, kind });
    setOperationInput(json(body));
    setOperationResult(null);
  }

  async function submitOperation() {
    if (!operation) return;
    await run(async () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(operationInput);
      } catch {
        throw new Error(t("longterm.jsonObjectRequired"));
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
        throw new Error(t("longterm.jsonObjectRequired"));
      const body = parsed as Record<string, unknown>;
      const paths = credentialPaths(body);
      if (paths.length)
        throw new Error(
          t("longterm.credentialsBlocked", { paths: paths.join(", ") }),
        );
      const result = await longtermApi.taskOperation(
        operation.task.tenant_id,
        operation.task.task_id,
        operation.kind,
        {
          ...body,
          ...(["evaluate", "evolution"].includes(operation.kind)
            ? { expected_revision: operation.task.revision }
            : {}),
        },
      );
      setOperationResult(result);
      setRefreshKey((value) => value + 1);
      message.success(t("longterm.operationCompleted"));
    });
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
        defaultActiveKey="workflow"
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
                      render: (value: string) => value || "—",
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
            key: "integrations",
            label: t("longterm.integrationsTab"),
            children: (
              <div className={styles.content}>
                <Alert
                  type="info"
                  showIcon
                  message={t("longterm.integrationHint")}
                />
                <Table
                  rowKey="id"
                  dataSource={integrations}
                  pagination={false}
                  scroll={{ x: 620 }}
                  locale={{ emptyText: t("longterm.noIntegrations") }}
                  columns={[
                    {
                      title: t("longterm.integration"),
                      dataIndex: "name",
                      render: (name: string, row: IntegrationStatus) => (
                        <>
                          <Text strong>{name}</Text>
                          <br />
                          <Text type="secondary">{row.id}</Text>
                        </>
                      ),
                    },
                    {
                      title: t("longterm.status"),
                      dataIndex: "status",
                      render: (status: string) => (
                        <Tag>
                          {t(`longterm.integrationStatuses.${status}`, {
                            defaultValue: status,
                          })}
                        </Tag>
                      ),
                    },
                    {
                      title: t("longterm.detail"),
                      dataIndex: "detail",
                      render: (value: string) =>
                        t(`longterm.integrationDetails.${value}`, {
                          defaultValue: value,
                        }),
                    },
                  ]}
                />
              </div>
            ),
          },
        ]}
      />
    </div>
  );

  const headerActions = (
    <Space wrap>
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
        open={!!nameDialog}
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
        open={!!nodeDialog}
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
                      : Promise.reject(new Error(t("longterm.agentRequired"))),
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
            >
              <Input />
            </Form.Item>
          </div>
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
              options={["R0", "R1", "R2", "R3"].map((risk) => ({
                value: risk,
                label: t(`longterm.risks.${risk}`),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="input_json"
            label={t("longterm.inputJson")}
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
        open={beatdesignOpen}
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
        open={!!detail}
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
        open={!!approval}
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
        open={!!membersTenant}
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
      <Modal
        title={operation ? t(`longterm.operationNames.${operation.kind}`) : ""}
        open={!!operation}
        onCancel={() => setOperation(null)}
        onOk={() => void submitOperation()}
        confirmLoading={busy}
        okText={t("longterm.submitOperation")}
        okButtonProps={{ disabled: operationResult !== null }}
        width={760}
      >
        {operation && (
          <div className={styles.content}>
            <Alert
              type="info"
              showIcon
              message={t(`longterm.operationHints.${operation.kind}`)}
            />
            <Form layout="vertical">
              <Form.Item label={t("longterm.operationInput")}>
                <Input.TextArea
                  className={styles.codeInput}
                  rows={10}
                  value={operationInput}
                  onChange={(event) => {
                    setOperationInput(event.target.value);
                    setOperationResult(null);
                  }}
                />
              </Form.Item>
            </Form>
            {operationResult !== null && (
              <>
                <Text strong>{t("longterm.operationResult")}</Text>
                <pre className={styles.json}>{json(operationResult)}</pre>
              </>
            )}
          </div>
        )}
      </Modal>
    </>
  );
}
