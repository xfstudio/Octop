import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { ManagedComponentId } from "../../api/modules/componentManagement";
import type { LongtermTask, WorkflowNode } from "../../api/modules/longterm";
import {
  operationSessionsApi,
  type OperationSession,
  type SessionOperation,
} from "../../api/modules/operationSessions";
import { apiErrorMessage } from "../../utils/apiError";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import {
  DEFAULT_COMPONENT_OPERATIONS,
  localIdentifier,
} from "./localWorkspace";
import { COMPONENTS } from "./componentMetadata";
import {
  SessionResultError,
  sessionConsoleHref,
  sessionDraftInput,
  validateDraftResult,
} from "./sessionResults";
import styles from "./ComponentOperations.module.less";

export interface OperationSessionSeed {
  component_id: ManagedComponentId;
  operation?: string;
  input?: Record<string, unknown>;
  task_id?: string;
  node_id?: string;
  session_id?: string;
  initial_result?: unknown;
}

const fallbackOperations: SessionOperation[] = [
  ...DEFAULT_COMPONENT_OPERATIONS.filter(
    (entry) => entry.component_id !== "semantica",
  ).map((entry) => ({
    ...entry,
    component_id: entry.component_id as ManagedComponentId,
  })),
  {
    component_id: "octop",
    operation: "task",
    input_example: {},
    task_required: true,
  },
  {
    component_id: "octop",
    operation: "node_result",
    input_example: {},
    task_required: true,
    node_required: true,
  },
  {
    component_id: "semantica",
    operation: "receipt",
    input_example: {},
    task_required: true,
  },
  {
    component_id: "temporal",
    operation: "status",
    input_example: {},
    task_required: true,
  },
];
const json = (value: unknown) => JSON.stringify(value, null, 2);

export default function OperationSessionDialog({
  tenantId,
  spaceId,
  seed,
  tasks,
  draftNodes,
  connected,
  onClose,
  onApply,
}: {
  tenantId: string;
  spaceId: string;
  seed: OperationSessionSeed;
  tasks: LongtermTask[];
  draftNodes: WorkflowNode[];
  connected: boolean;
  onClose: () => void;
  onApply: (
    input: Record<string, unknown>,
    targetId?: string,
  ) => boolean | void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone(connected);
  const tr = (key: string) => t(`operationSessions.${key}`);
  const [operations, setOperations] = useState(fallbackOperations);
  const [sessions, setSessions] = useState<OperationSession[]>([]);
  const [session, setSession] = useState<OperationSession | null>(null);
  const [componentId, setComponentId] = useState(seed.component_id);
  const [operation, setOperation] = useState(
    seed.operation ??
      fallbackOperations.find(
        (entry) => entry.component_id === seed.component_id,
      )?.operation ??
      "task",
  );
  const [inputJson, setInputJson] = useState(
    json(
      seed.input ??
        fallbackOperations.find(
          (entry) => entry.component_id === seed.component_id,
        )?.input_example ??
        {},
    ),
  );
  const [taskId, setTaskId] = useState(seed.task_id ?? "");
  const [nodeId, setNodeId] = useState(seed.node_id ?? "");
  const [importJson, setImportJson] = useState(
    seed.initial_result === undefined ? "" : json(seed.initial_result),
  );
  const [importConfirmed, setImportConfirmed] = useState(false);
  const [commitConfirmed, setCommitConfirmed] = useState(false);
  const [pointer, setPointer] = useState("/output");
  const [targetId, setTargetId] = useState("");
  const [replaceConfirmed, setReplaceConfirmed] = useState(false);
  const [applied, setApplied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(true);
  const locked = useRef(false);
  const controllers = useRef(new Set<AbortController>());
  const retryKeys = useRef(new Map<string, string>());
  const scopeTasks = tasks.filter(
    (task) => task.tenant_id === tenantId && task.space_id === spaceId,
  );
  const selectedTask = scopeTasks.find((task) => task.task_id === taskId);
  const descriptor = operations.find(
    (entry) =>
      entry.component_id === componentId && entry.operation === operation,
  );
  const candidate = session?.result ?? session?.candidate;
  const href = session
    ? sessionConsoleHref(session, window.location.origin)
    : null;
  let preview: Record<string, unknown> | undefined;
  let previewError = "";
  if (session?.status === "committed") {
    try {
      preview = sessionDraftInput(session, pointer);
    } catch (cause) {
      previewError =
        cause instanceof SessionResultError
          ? tr(`errors.${cause.key}`)
          : tr("failed");
    }
  }

  useEffect(() => {
    active.current = true;
    if (!connected) return;
    const controller = new AbortController();
    const requests = controllers.current;
    requests.add(controller);
    void Promise.allSettled([
      operationSessionsApi.operations(controller.signal),
      operationSessionsApi.list(tenantId, spaceId, controller.signal),
      seed.session_id
        ? operationSessionsApi.get(
            tenantId,
            spaceId,
            seed.session_id,
            controller.signal,
          )
        : Promise.resolve(null),
    ])
      .then(([catalog, list, current]) => {
        if (!active.current || controller.signal.aborted) return;
        if (catalog.status === "fulfilled" && catalog.value.operations.length)
          setOperations(catalog.value.operations);
        if (list.status === "fulfilled") setSessions(list.value.sessions);
        if (current.status === "fulfilled" && current.value)
          setSession(current.value);
        if (list.status === "rejected" || current.status === "rejected")
          setError(tr("loadFailed"));
      })
      .finally(() => requests.delete(controller));
    return () => {
      active.current = false;
      requests.forEach((request) => request.abort());
      requests.clear();
    };
    // The parent keys this dialog by actor, tenant, space and seed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, spaceId, connected, seed.session_id]);

  const run = async (
    action: (signal: AbortSignal) => Promise<OperationSession>,
  ) => {
    if (locked.current || !connected) return;
    locked.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    controllers.current.add(controller);
    try {
      const next = await action(controller.signal);
      if (!active.current || controller.signal.aborted) return;
      setSession(next);
      setSessions((previous) => [
        next,
        ...previous.filter((item) => item.session_id !== next.session_id),
      ]);
      setCommitConfirmed(false);
      setReplaceConfirmed(false);
      setApplied(false);
    } catch (cause) {
      if (active.current && !controller.signal.aborted)
        setError(
          cause instanceof SessionResultError
            ? tr(`errors.${cause.key}`)
            : apiErrorMessage(cause, tr("failed"), t),
        );
    } finally {
      controllers.current.delete(controller);
      locked.current = false;
      if (active.current) setBusy(false);
    }
  };
  const idempotencyKey = (payload: unknown) => {
    const key = JSON.stringify(payload);
    if (!retryKeys.current.has(key))
      retryKeys.current.set(key, localIdentifier());
    return retryKeys.current.get(key)!;
  };
  const create = () =>
    run(async (signal) => {
      let input: unknown;
      try {
        input = JSON.parse(inputJson);
      } catch {
        throw new SessionResultError("json");
      }
      validateDraftResult(input);
      if (input === null || typeof input !== "object" || Array.isArray(input))
        throw new SessionResultError("json");
      const body = {
        component_id: componentId,
        operation,
        input: input as Record<string, unknown>,
        ...(taskId ? { task_id: taskId } : {}),
        ...(taskId && nodeId ? { node_id: nodeId } : {}),
      };
      const session = await operationSessionsApi.create(
        tenantId,
        spaceId,
        { ...body, idempotency_key: idempotencyKey({ create: body }) },
        signal,
      );
      retryKeys.current.delete(JSON.stringify({ create: body }));
      return session;
    });
  const action = (kind: "readback" | "import" | "commit" | "cancel") => {
    if (!session) return;
    void run(async (signal) => {
      const extra: { result?: unknown; result_digest?: string } = {};
      if (kind === "import") {
        if (!importConfirmed) throw new SessionResultError("json");
        try {
          extra.result = JSON.parse(importJson);
        } catch {
          throw new SessionResultError("json");
        }
        validateDraftResult(extra.result);
      }
      if (kind === "commit") {
        if (!commitConfirmed || !session.result_digest)
          throw new SessionResultError("json");
        extra.result_digest = session.result_digest;
      }
      return operationSessionsApi.action(
        tenantId,
        spaceId,
        session.session_id,
        kind,
        {
          expected_revision: session.revision,
          idempotency_key: idempotencyKey({
            id: session.session_id,
            revision: session.revision,
            kind,
            ...extra,
          }),
          ...extra,
        },
        signal,
      );
    });
  };
  const chooseComponent = (id: ManagedComponentId) => {
    const first = operations.find((entry) => entry.component_id === id);
    if (!first) return;
    setComponentId(id);
    setOperation(first.operation);
    setInputJson(json(first.input_example));
  };
  const canCreate =
    connected &&
    !!descriptor &&
    (!descriptor.task_required || !!taskId) &&
    (!descriptor.node_required || !!nodeId);

  return (
    <Modal
      open
      title={tr("title")}
      onCancel={onClose}
      footer={null}
      width={880}
      className={styles.modal}
    >
      <div className={styles.content}>
        <Alert
          type="info"
          showIcon
          message={tr("draftOnly")}
          description={tr("returnHint")}
        />
        {!connected && (
          <Alert type="warning" showIcon message={tr("offline")} />
        )}
        {error && <Alert type="error" showIcon message={error} />}
        <Form layout="vertical" disabled={busy || !connected}>
          <Form.Item label={tr("recent")}>
            <Select
              aria-label={tr("recent")}
              allowClear
              value={session?.session_id}
              options={sessions.map((item) => ({
                value: item.session_id,
                label: `${item.component_id} / ${item.operation} · ${tr(
                  `statuses.${item.status}`,
                )} · ${formatServerDateTime(item.updated_at, timeZone)}`,
              }))}
              onChange={(id: string | undefined) => {
                if (id)
                  void run((signal) =>
                    operationSessionsApi.get(tenantId, spaceId, id, signal),
                  );
                else setSession(null);
              }}
            />
          </Form.Item>
          {!session ? (
            <>
              <Space wrap>
                <Form.Item label={tr("component")}>
                  <Select
                    aria-label={tr("component")}
                    value={componentId}
                    style={{ minWidth: 170 }}
                    options={COMPONENTS.filter((item) =>
                      operations.some(
                        (entry) => entry.component_id === item.id,
                      ),
                    ).map((item) => ({ value: item.id, label: item.name }))}
                    onChange={chooseComponent}
                  />
                </Form.Item>
                <Form.Item label={tr("operation")}>
                  <Select
                    aria-label={tr("operation")}
                    value={operation}
                    style={{ minWidth: 200 }}
                    options={operations
                      .filter((entry) => entry.component_id === componentId)
                      .map((entry) => ({
                        value: entry.operation,
                        label: entry.operation,
                      }))}
                    onChange={(name: string) => {
                      setOperation(name);
                      setInputJson(
                        json(
                          operations.find(
                            (entry) =>
                              entry.component_id === componentId &&
                              entry.operation === name,
                          )?.input_example ?? {},
                        ),
                      );
                    }}
                  />
                </Form.Item>
              </Space>
              <Form.Item label={tr("task")}>
                <Select
                  aria-label={tr("task")}
                  allowClear
                  value={taskId || undefined}
                  options={scopeTasks
                    .filter(
                      (task) =>
                        componentId !== "temporal" || task.mode === "temporal",
                    )
                    .map((task) => ({ value: task.task_id, label: task.goal }))}
                  onChange={(id: string | undefined) => {
                    setTaskId(id ?? "");
                    setNodeId("");
                  }}
                />
              </Form.Item>
              {selectedTask && (
                <Form.Item label={tr("node")}>
                  <Select
                    aria-label={tr("node")}
                    allowClear
                    value={nodeId || undefined}
                    options={selectedTask.workflow.nodes.map((node) => ({
                      value: node.id,
                      label: node.id,
                    }))}
                    onChange={(id: string | undefined) => setNodeId(id ?? "")}
                  />
                </Form.Item>
              )}
              <Form.Item label={tr("input")} extra={tr("inputHint")}>
                <Input.TextArea
                  aria-label={tr("input")}
                  rows={4}
                  value={inputJson}
                  onChange={(event) => setInputJson(event.target.value)}
                />
              </Form.Item>
              <Button
                type="primary"
                disabled={!canCreate}
                loading={busy}
                onClick={() => void create()}
              >
                {tr("create")}
              </Button>
            </>
          ) : (
            <>
              <Descriptions
                size="small"
                column={1}
                items={[
                  {
                    key: "id",
                    label: tr("sessionId"),
                    children: (
                      <Typography.Text code copyable>
                        {session.session_id}
                      </Typography.Text>
                    ),
                  },
                  {
                    key: "operation",
                    label: tr("operation"),
                    children: `${session.component_id} / ${session.operation}`,
                  },
                  {
                    key: "status",
                    label: tr("status"),
                    children: <Tag>{tr(`statuses.${session.status}`)}</Tag>,
                  },
                  {
                    key: "scope",
                    label: tr("scope"),
                    children: `${tenantId} / ${spaceId}${
                      session.task_id ? ` / ${session.task_id}` : ""
                    }${session.node_id ? ` / ${session.node_id}` : ""}`,
                  },
                  {
                    key: "time",
                    label: tr("updatedAt"),
                    children: formatServerDateTime(
                      session.updated_at,
                      timeZone,
                    ),
                  },
                ]}
              />
              <Space wrap>
                {href &&
                  (session.snapshot.console?.kind === "internal" ? (
                    <Link to={href} target="_blank" rel="noopener noreferrer">
                      {tr("openConsole")}
                    </Link>
                  ) : (
                    <a href={href} target="_blank" rel="noopener noreferrer">
                      {tr("openConsole")}
                    </a>
                  ))}
                <Button
                  disabled={!["created", "ready"].includes(session.status)}
                  loading={busy}
                  onClick={() => action("readback")}
                >
                  {tr("readback")}
                </Button>
                <Button onClick={() => setSession(null)}>
                  {tr("newSession")}
                </Button>
                {["created", "ready"].includes(session.status) && (
                  <Button danger onClick={() => action("cancel")}>
                    {tr("cancel")}
                  </Button>
                )}
              </Space>
              {["created", "ready"].includes(session.status) && (
                <Card size="small" title={tr("manualImport")}>
                  <Alert
                    type="warning"
                    showIcon
                    message={tr("unverifiedHint")}
                  />
                  <Input.TextArea
                    aria-label={tr("importJson")}
                    rows={4}
                    value={importJson}
                    onChange={(event) => {
                      setImportJson(event.target.value);
                      setImportConfirmed(false);
                    }}
                  />
                  <Checkbox
                    checked={importConfirmed}
                    onChange={(event) =>
                      setImportConfirmed(event.target.checked)
                    }
                  >
                    {tr("importConfirm")}
                  </Checkbox>
                  <Button
                    disabled={!importConfirmed || !importJson.trim()}
                    onClick={() => action("import")}
                  >
                    {tr("import")}
                  </Button>
                </Card>
              )}
            </>
          )}
        </Form>
        {candidate && (
          <Card size="small" title={tr("preview")}>
            <Descriptions
              size="small"
              column={1}
              items={[
                {
                  key: "source",
                  label: tr("source"),
                  children: candidate.source,
                },
                {
                  key: "verified",
                  label: tr("verified"),
                  children: (
                    <Tag color={candidate.verified ? "green" : "orange"}>
                      {tr(
                        candidate.verified
                          ? "verifiedReadback"
                          : "manualUnverified",
                      )}
                    </Tag>
                  ),
                },
                {
                  key: "digest",
                  label: tr("digest"),
                  children: (
                    <Typography.Text code copyable className={styles.digest}>
                      {session?.result_digest ?? "—"}
                    </Typography.Text>
                  ),
                },
              ]}
            />
            <pre className={styles.json}>{json(candidate)}</pre>
            {session?.status === "ready" && (
              <Space direction="vertical">
                <Checkbox
                  disabled={busy || !connected}
                  checked={commitConfirmed}
                  onChange={(event) => setCommitConfirmed(event.target.checked)}
                >
                  {tr("commitConfirm")}
                </Checkbox>
                <Button
                  type="primary"
                  disabled={!commitConfirmed || busy || !connected}
                  onClick={() => action("commit")}
                >
                  {tr("commit")}
                </Button>
              </Space>
            )}
          </Card>
        )}
        {session?.status === "committed" && (
          <Card size="small" title={tr("applyTitle")}>
            <Form layout="vertical">
              <Form.Item label={tr("pointer")} extra={tr("pointerHint")}>
                <Input
                  aria-label={tr("pointer")}
                  value={pointer}
                  onChange={(event) => {
                    setPointer(event.target.value);
                    setReplaceConfirmed(false);
                    setApplied(false);
                  }}
                />
              </Form.Item>
              <Form.Item label={tr("target")}>
                <Select
                  aria-label={tr("target")}
                  value={targetId}
                  options={[
                    { value: "", label: tr("newInputNode") },
                    ...draftNodes.map((node) => ({
                      value: node.id,
                      label: node.id,
                    })),
                  ]}
                  onChange={(id: string) => {
                    setTargetId(id);
                    setReplaceConfirmed(false);
                    setApplied(false);
                  }}
                />
              </Form.Item>
              {targetId && (
                <>
                  <Typography.Text type="warning">
                    {tr("replaceWarning")}
                  </Typography.Text>
                  <pre className={styles.json}>
                    {json(
                      draftNodes.find((node) => node.id === targetId)?.input,
                    )}
                  </pre>
                  <Checkbox
                    checked={replaceConfirmed}
                    onChange={(event) =>
                      setReplaceConfirmed(event.target.checked)
                    }
                  >
                    {tr("replaceConfirm")}
                  </Checkbox>
                </>
              )}
              {previewError ? (
                <Alert type="error" message={previewError} />
              ) : (
                <pre className={styles.json}>{json(preview)}</pre>
              )}
              <Button
                type="primary"
                disabled={
                  !preview ||
                  !!previewError ||
                  (targetId !== "" && !replaceConfirmed) ||
                  applied
                }
                onClick={() => {
                  if (
                    preview &&
                    onApply(preview, targetId || undefined) !== false
                  )
                    setApplied(true);
                }}
              >
                {tr(targetId ? "replaceInput" : "appendInput")}
              </Button>
              {applied && (
                <Alert type="success" showIcon message={tr("applied")} />
              )}
            </Form>
          </Card>
        )}
        <Button onClick={onClose}>{tr("close")}</Button>
      </div>
    </Modal>
  );
}
