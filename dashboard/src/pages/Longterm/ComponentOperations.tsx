import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  longtermApi,
  type IntegrationStatus,
  type LongtermTask,
  type TaskOperation,
  type Tenant,
} from "../../api/modules/longterm";
import { useCurrentUser } from "../../hooks/useCurrentUser";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../utils/formatMessageTime";
import { apiErrorMessage } from "../../utils/apiError";
import {
  artifactRecords,
  asRecord,
  buildOperationBody,
  candidateActions,
  frozenBaseline,
  latestCandidates,
  OperationValidationError,
  type EvolutionAction,
  type OperationValues,
} from "./componentOperationsHelpers";
import styles from "./ComponentOperations.module.less";

const { Text, Paragraph } = Typography;
const actions: EvolutionAction[] = [
  "propose",
  "rna_propose",
  "assess",
  "shadow",
  "canary",
  "promoted",
  "rolled_back",
];
const componentIds: Record<TaskOperation, string> = {
  evaluate: "commerceagentbench",
  memory: "hindsight",
  provenance: "semantica",
  evolution: "rna",
};
const display = (value: unknown) =>
  typeof value === "number" || typeof value === "string" ? String(value) : "—";
const json = (value: unknown) => JSON.stringify(value, null, 2);

export interface ComponentOperationsProps {
  task: LongtermTask;
  kind: TaskOperation;
  role?: Tenant["role"];
  integration?: IntegrationStatus;
  onCancel: () => void;
  onSuccess: (result: unknown) => void;
  onManageComponent?: (componentId: string) => void;
  onReturnResult?: (result: unknown, input?: Record<string, unknown>) => void;
}

/** A task switch unmounts the previous request scope, including its form and result. */
export default function ComponentOperations(props: ComponentOperationsProps) {
  const user = useCurrentUser();
  return (
    <OperationDialog
      key={`${props.task.tenant_id}:${props.task.task_id}:${props.kind}:${user?.id}:${props.role}`}
      {...props}
    />
  );
}

function OperationDialog({
  task,
  kind,
  role,
  integration,
  onCancel,
  onSuccess,
  onManageComponent,
  onReturnResult,
}: ComponentOperationsProps) {
  const { t } = useTranslation();
  const tr = (key: string) => t(`componentOperations.${key}`);
  const user = useCurrentUser();
  const [form] = Form.useForm<OperationValues>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<unknown>(null);
  const [recordedTask, setRecordedTask] = useState(task);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const scope = `${task.tenant_id}:${task.task_id}:${user?.id}:${role}`;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const currentTask =
    recordedTask.revision > task.revision ? recordedTask : task;
  const action =
    Form.useWatch("action", form) ?? (kind === "memory" ? "recall" : "propose");
  const admin = role === "owner" || role === "admin";
  const writer = admin || role === "member";
  const permitted =
    !!user &&
    !!role &&
    (kind === "evolution" || (kind === "memory" && action !== "recall")
      ? admin
      : kind === "memory"
      ? true
      : writer);
  const unavailable =
    integration &&
    ["unconfigured", "disabled", "unavailable"].includes(integration.status) &&
    (kind !== "evolution" || action === "rna_propose");
  const verification = asRecord(currentTask.checkpoint?.verification);
  const verifiedMemory =
    asRecord(currentTask.result).goal_outcome === "achieved" &&
    Array.isArray(verification.evidence_refs) &&
    verification.evidence_refs.length > 0;
  const retainBlocked =
    kind === "memory" && action === "retain" && !verifiedMemory;
  const candidates = latestCandidates(currentTask);
  const eligibleCandidates = candidates.filter((candidate) =>
    candidateActions(candidate.status).includes(action as EvolutionAction),
  );
  const requiresConfirmation =
    kind === "evolution" &&
    ["rna_propose", "canary", "promoted", "rolled_back"].includes(action);
  const needsCandidate =
    kind === "evolution" && !["propose", "rna_propose"].includes(action);
  const consentRequired =
    kind === "provenance" || (kind === "memory" && action !== "recall");
  const selectedId = Form.useWatch("candidate_id", form);
  const currentCandidate = candidates.find(
    (candidate) => candidate.id === selectedId,
  );

  const submit = async (values: OperationValues) => {
    if (inFlight.current || !permitted || unavailable || retainBlocked) return;
    let body: Record<string, unknown>;
    try {
      body = buildOperationBody(kind, currentTask, values);
    } catch (cause) {
      setError(
        cause instanceof OperationValidationError
          ? tr(`validation.${cause.key}`)
          : tr("failed"),
      );
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError("");
    setResult(null);
    const requestScope = scope;
    try {
      const response = await longtermApi.taskOperation(
        task.tenant_id,
        task.task_id,
        kind,
        body,
      );
      if (!mounted.current || currentScope.current !== requestScope) return;
      setResult(response);
      const responseTask = asRecord(asRecord(response).task);
      if (
        responseTask.task_id === task.task_id &&
        typeof responseTask.revision === "number"
      ) {
        setRecordedTask(responseTask as unknown as LongtermTask);
      }
      if (asRecord(response).status !== "blocked") onSuccess(response);
      form.setFieldsValue({ consent: false, confirm: false });
    } catch (cause) {
      if (mounted.current && currentScope.current === requestScope)
        setError(apiErrorMessage(cause, tr("failed"), t));
    } finally {
      inFlight.current = false;
      if (mounted.current && currentScope.current === requestScope)
        setBusy(false);
    }
  };
  const required = {
    required: true,
    whitespace: true,
    message: tr("required"),
  };
  const label = (name: string) => tr(`fields.${name}`);
  const operationName =
    kind === "evaluate" || kind === "provenance"
      ? tr(`submit.${kind}`)
      : tr(`actions.${action}`);

  return (
    <Modal
      open
      title={tr(`titles.${kind}`)}
      onCancel={onCancel}
      footer={null}
      width={820}
      className={styles.modal}
    >
      <div className={styles.content}>
        <Descriptions
          size="small"
          column={1}
          items={[
            {
              key: "task",
              label: tr("task"),
              children: (
                <Text code copyable>
                  {task.task_id}
                </Text>
              ),
            },
            { key: "goal", label: tr("goal"), children: task.goal },
            {
              key: "revision",
              label: tr("revision"),
              children: currentTask.revision,
            },
          ]}
        />
        <Alert
          showIcon
          type={unavailable ? "warning" : "info"}
          message={tr(`prerequisites.${kind}`)}
          description={tr(unavailable ? "unconfigured" : "connectionUnknown")}
          action={
            onManageComponent && (
              <Button
                size="small"
                onClick={() => {
                  onCancel();
                  onManageComponent(componentIds[kind]);
                }}
              >
                {tr("manage")}
              </Button>
            )
          }
        />
        {!permitted && (
          <Alert
            showIcon
            type="warning"
            message={tr(
              kind === "evolution" || (kind === "memory" && action !== "recall")
                ? "adminRequired"
                : "permissionRequired",
            )}
          />
        )}
        {retainBlocked && (
          <Alert
            showIcon
            type="warning"
            message={tr("verifiedMemoryRequired")}
          />
        )}
        {error && <Alert showIcon type="error" message={error} />}
        <Form
          form={form}
          layout="vertical"
          initialValues={{
            action: kind === "memory" ? "recall" : "propose",
            max_tokens: 2048,
            ttl_seconds: 2592000,
            strategy: frozenBaseline(task).strategy,
            consent: false,
            confirm: false,
          }}
          onFinish={(values) => void submit(values)}
          onValuesChange={(changed) => {
            setError("");
            if ("action" in changed) {
              form.setFieldsValue({
                candidate_id: undefined,
                consent: false,
                confirm: false,
              });
              setResult(null);
            } else if (!("confirm" in changed) && !("consent" in changed))
              form.setFieldsValue({ confirm: false, consent: false });
          }}
          disabled={busy}
        >
          {kind === "evaluate" && (
            <>
              <Form.Item
                name="run_dir"
                label={label("runDir")}
                rules={[required]}
                extra={tr("runDirHint")}
              >
                <Input maxLength={400} placeholder="bm-smoke/run-001" />
              </Form.Item>
              <Form.Item
                name="benchmark_task_id"
                label={label("benchmarkTask")}
                extra={tr("benchmarkTaskHint")}
              >
                <Input maxLength={200} />
              </Form.Item>
            </>
          )}
          {kind === "memory" && (
            <>
              <Form.Item name="action" label={label("memoryAction")}>
                <Select
                  options={["recall", "retain", "delete"].map((value) => ({
                    value,
                    label: tr(`actions.${value}`),
                    disabled: value !== "recall" && !admin,
                  }))}
                />
              </Form.Item>
              {action === "recall" && (
                <>
                  <Form.Item
                    name="query"
                    label={label("query")}
                    rules={[required]}
                  >
                    <Input.TextArea rows={3} maxLength={8000} />
                  </Form.Item>
                  <Form.Item name="max_tokens" label={label("maxTokens")}>
                    <InputNumber min={128} max={4096} precision={0} />
                  </Form.Item>
                </>
              )}
              {action === "retain" && (
                <>
                  <Form.Item
                    name="content"
                    label={label("content")}
                    rules={[required]}
                  >
                    <Input.TextArea rows={5} maxLength={16000} />
                  </Form.Item>
                  <Form.Item
                    name="ttl_seconds"
                    label={label("ttl")}
                    extra={tr("ttlHint")}
                  >
                    <InputNumber min={60} max={31536000} precision={0} />
                  </Form.Item>
                </>
              )}
              {action === "delete" && (
                <>
                  <Alert showIcon type="warning" message={tr("deleteHint")} />
                  <Form.Item
                    name="document_id"
                    label={label("documentId")}
                    rules={[required]}
                  >
                    <Input maxLength={200} placeholder="octop-…" />
                  </Form.Item>
                </>
              )}
            </>
          )}
          {kind === "evolution" && (
            <>
              <Alert
                showIcon
                type="info"
                message={tr("trustedReports")}
                description={tr("inertCandidate")}
              />
              <Form.Item name="action" label={label("evolutionAction")}>
                <Select
                  options={actions.map((value) => ({
                    value,
                    label: tr(`actions.${value}`),
                    disabled:
                      !["propose", "rna_propose"].includes(value) &&
                      !candidates.some((candidate) =>
                        candidateActions(candidate.status).includes(value),
                      ),
                  }))}
                />
              </Form.Item>
              {["propose", "rna_propose"].includes(action) && (
                <Form.Item
                  name="benchmark_revision"
                  label={label("benchmarkRevision")}
                  rules={[required]}
                  extra={tr("benchmarkRevisionHint")}
                >
                  <Input maxLength={200} />
                </Form.Item>
              )}
              {action === "propose" && (
                <>
                  <Form.Item
                    name="summary"
                    label={label("summary")}
                    rules={[required]}
                  >
                    <Input maxLength={8000} />
                  </Form.Item>
                  <Form.Item
                    name="hypothesis"
                    label={label("hypothesis")}
                    rules={[required]}
                  >
                    <Input.TextArea rows={3} maxLength={8000} />
                  </Form.Item>
                  <Form.Item name="strategy" label={label("strategy")}>
                    <Input.TextArea rows={4} maxLength={32000} />
                  </Form.Item>
                  <Collapse
                    items={[
                      {
                        key: "advanced",
                        label: tr("advancedProposal"),
                        children: (
                          <Form.Item
                            name="advanced_proposal"
                            label={label("proposalFields")}
                            extra={tr("proposalHint")}
                          >
                            <Input.TextArea
                              className={styles.code}
                              rows={6}
                              maxLength={128000}
                              placeholder={'{"skillUpdates": []}'}
                            />
                          </Form.Item>
                        ),
                      },
                    ]}
                  />
                </>
              )}
              {action === "rna_propose" && (
                <>
                  <Form.Item
                    name="strategy"
                    label={label("baselineStrategy")}
                    rules={[required]}
                  >
                    <Input.TextArea rows={5} maxLength={32000} />
                  </Form.Item>
                  <Text type="secondary">{tr("baselineSkillsHint")}</Text>
                  <Table
                    size="small"
                    rowKey="id"
                    pagination={false}
                    dataSource={frozenBaseline(currentTask).skills}
                    columns={[
                      { title: label("skillId"), dataIndex: "id" },
                      {
                        title: label("instructions"),
                        dataIndex: "instructions",
                        ellipsis: true,
                      },
                    ]}
                    locale={{ emptyText: tr("noFrozenSkills") }}
                  />
                </>
              )}
              {needsCandidate && (
                <Form.Item
                  name="candidate_id"
                  label={label("candidate")}
                  rules={[required]}
                  extra={tr("candidateHint")}
                >
                  <Select
                    options={eligibleCandidates.map((candidate) => ({
                      value: String(candidate.id),
                      label: `${display(
                        asRecord(candidate.proposal).summary,
                      )} · ${display(candidate.id)}`,
                    }))}
                  />
                </Form.Item>
              )}
              {currentCandidate && needsCandidate && (
                <Descriptions
                  size="small"
                  column={1}
                  items={[
                    {
                      key: "status",
                      label: tr("status"),
                      children: tr(`statuses.${currentCandidate.status}`),
                    },
                    {
                      key: "baseline",
                      label: label("baselineRevision"),
                      children: (
                        <Text code className={styles.digest}>
                          {display(currentCandidate.baseline_revision)}
                        </Text>
                      ),
                    },
                    {
                      key: "benchmark",
                      label: label("benchmarkRevision"),
                      children: display(currentCandidate.benchmark_revision),
                    },
                    {
                      key: "gate",
                      label: tr("gate"),
                      children: display(
                        asRecord(currentCandidate.evaluation).gate,
                      ),
                    },
                  ]}
                />
              )}
            </>
          )}
          {kind === "provenance" && (
            <Paragraph>{tr("provenanceHint")}</Paragraph>
          )}
          {consentRequired && (
            <Form.Item
              name="consent"
              valuePropName="checked"
              rules={[
                {
                  validator: (_, checked) =>
                    checked
                      ? Promise.resolve()
                      : Promise.reject(
                          new Error(tr("validation.consentRequired")),
                        ),
                },
              ]}
            >
              <Checkbox>
                {tr(
                  kind === "provenance"
                    ? "consentExport"
                    : action === "delete"
                    ? "consentDelete"
                    : "consentRetain",
                )}
              </Checkbox>
            </Form.Item>
          )}
          {requiresConfirmation && (
            <Form.Item
              name="confirm"
              valuePropName="checked"
              rules={[
                {
                  validator: (_, checked) =>
                    checked
                      ? Promise.resolve()
                      : Promise.reject(
                          new Error(tr("validation.confirmRequired")),
                        ),
                },
              ]}
            >
              <Checkbox>
                {tr(
                  action === "rna_propose" ? "confirmRna" : "confirmTransition",
                )}
              </Checkbox>
            </Form.Item>
          )}
          <Space className={styles.footer} wrap>
            <Button onClick={onCancel}>{tr("close")}</Button>
            <Button
              type="primary"
              danger={action === "delete" || action === "rolled_back"}
              htmlType="submit"
              loading={busy}
              disabled={
                !permitted ||
                !!unavailable ||
                retainBlocked ||
                (needsCandidate && !eligibleCandidates.length)
              }
            >
              {operationName}
            </Button>
          </Space>
        </Form>
        {result !== null && (
          <>
            <OperationResult kind={kind} value={result} />
            {onReturnResult && asRecord(result).status !== "blocked" && (
              <Button
                onClick={() =>
                  onReturnResult(
                    asRecord(result).record ?? result,
                    kind === "evaluate"
                      ? {
                          run_dir: form.getFieldValue("run_dir"),
                          ...(form.getFieldValue("benchmark_task_id")
                            ? {
                                expected_task_id:
                                  form.getFieldValue("benchmark_task_id"),
                              }
                            : {}),
                        }
                      : kind === "memory"
                      ? {
                          query: form.getFieldValue("query") || task.goal,
                          max_tokens: form.getFieldValue("max_tokens") ?? 2048,
                        }
                      : {},
                  )
                }
              >
                {t("operationSessions.saveResult")}
              </Button>
            )}
          </>
        )}
        {(kind === "evaluate" || kind === "evolution") && (
          <ComponentArtifacts
            task={currentTask}
            group={kind === "evaluate" ? "evaluation" : "evolution"}
          />
        )}
      </div>
    </Modal>
  );
}

function OperationResult({
  kind,
  value,
}: {
  kind: TaskOperation;
  value: unknown;
}) {
  const { t } = useTranslation();
  const tr = (key: string) => t(`componentOperations.${key}`);
  const result = asRecord(value);
  if (result.status === "blocked")
    return (
      <Alert
        showIcon
        type="warning"
        message={tr("blocked")}
        description={display(result.reason)}
      />
    );
  if (kind === "evaluate" || kind === "evolution")
    return <Alert showIcon type="success" message={tr("recorded")} />;
  if (kind === "provenance")
    return (
      <Card size="small" title={tr("exportResult")}>
        <Descriptions
          size="small"
          column={1}
          items={[
            {
              key: "count",
              label: tr("recordCount"),
              children: display(result.count),
            },
            {
              key: "complete",
              label: tr("complete"),
              children: tr(result.complete === true ? "yes" : "no"),
            },
            {
              key: "head",
              label: tr("headDigest"),
              children: (
                <Text code copyable className={styles.digest}>
                  {display(result.head_digest)}
                </Text>
              ),
            },
          ]}
        />
      </Card>
    );
  const memories = Array.isArray(result.results)
    ? result.results.map(asRecord)
    : null;
  return (
    <Card size="small" title={tr("memoryResult")}>
      {memories ? (
        <>
          <Table
            size="small"
            pagination={{ pageSize: 5 }}
            rowKey={(row, index) => `${row.document_id ?? index}:${index}`}
            dataSource={memories}
            columns={[
              {
                title: tr("fields.documentId"),
                dataIndex: "document_id",
                render: (id: unknown) => (
                  <Text code copyable>
                    {display(id)}
                  </Text>
                ),
              },
              {
                title: tr("fields.content"),
                render: (_: unknown, row: Record<string, unknown>) => (
                  <Paragraph className={styles.memoryText}>
                    {display(row.text ?? row.content)}
                  </Paragraph>
                ),
              },
            ]}
            locale={{ emptyText: tr("noMemories") }}
          />
          <Text type="secondary">
            {tr("expiredFiltered")}:{" "}
            {Array.isArray(result.expired_document_ids)
              ? result.expired_document_ids.length
              : 0}
          </Text>
        </>
      ) : (
        <Descriptions
          size="small"
          column={1}
          items={[
            {
              key: "status",
              label: tr("status"),
              children: tr(
                result.deleted === true
                  ? "deleted"
                  : result.retained === true
                  ? "retained"
                  : "unknown",
              ),
            },
            {
              key: "document",
              label: tr("fields.documentId"),
              children: (
                <Text code copyable className={styles.digest}>
                  {display(result.document_id)}
                </Text>
              ),
            },
            {
              key: "bank",
              label: tr("bank"),
              children: (
                <Text code className={styles.digest}>
                  {display(result.bank_id)}
                </Text>
              ),
            },
          ]}
        />
      )}
    </Card>
  );
}

export function ComponentArtifacts({
  task,
  group,
}: {
  task: LongtermTask;
  group?: "evaluation" | "evolution";
}) {
  const { t } = useTranslation();
  const tr = (key: string) => t(`componentOperations.${key}`);
  const timezone = useServerTimezone();
  const evaluations = artifactRecords(task, "evaluation").slice().reverse();
  const candidates = latestCandidates(task);
  return (
    <div className={styles.content}>
      {group !== "evolution" && (
        <Card size="small" title={tr("evaluationHistory")}>
          <Alert type="info" showIcon message={tr("benchmarkScope")} />
          <Table
            size="small"
            scroll={{ x: 600 }}
            rowKey={(_, index) => String(index)}
            dataSource={evaluations}
            pagination={{ pageSize: 5 }}
            columns={[
              { title: tr("run"), dataIndex: "run_id", render: display },
              {
                title: tr("fields.benchmarkTask"),
                dataIndex: "task_id",
                render: display,
              },
              {
                title: tr("executionHealth"),
                render: (_, row) =>
                  tr(
                    `health.${
                      asRecord(row.execution_health).status ?? "unknown"
                    }`,
                  ),
              },
              {
                title: tr("goalOutcome"),
                dataIndex: "goal_outcome",
                render: (status: unknown) => (
                  <Tag
                    color={
                      status === "achieved"
                        ? "green"
                        : status === "not_achieved"
                        ? "red"
                        : "default"
                    }
                  >
                    {tr(`outcomes.${status ?? "unverified"}`)}
                  </Tag>
                ),
              },
              {
                title: tr("score"),
                dataIndex: "score",
                render: (score: unknown) =>
                  typeof score === "number" ? score.toFixed(3) : tr("unknown"),
              },
            ]}
            expandable={{
              expandedRowRender: (report) => {
                const metrics = asRecord(report.long_horizon_metrics);
                return (
                  <div className={styles.content}>
                    <Descriptions
                      column={{ xs: 1, sm: 2 }}
                      size="small"
                      items={[
                        "duration_ms",
                        "tool_calls",
                        "tool_errors",
                        "total_tokens",
                        "checks_passed",
                        "checks_total",
                      ].map((key) => ({
                        key,
                        label: tr(`metrics.${key}`),
                        children:
                          metrics[key] == null
                            ? tr("unknown")
                            : display(metrics[key]),
                      }))}
                    />
                    <Collapse
                      items={[
                        {
                          key: "source",
                          label: tr("sourceDetails"),
                          children: (
                            <pre className={styles.json}>
                              {json({
                                experiment: report.experiment,
                                provenance: report.provenance,
                                limitations: report.limitations,
                              })}
                            </pre>
                          ),
                        },
                      ]}
                    />
                  </div>
                );
              },
            }}
            locale={{ emptyText: tr("noEvaluations") }}
          />
        </Card>
      )}
      {group !== "evaluation" && (
        <Card size="small" title={tr("candidateHistory")}>
          <Text type="secondary">{tr("inertCandidate")}</Text>
          {!candidates.length ? (
            <Empty
              description={tr("noCandidates")}
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            candidates.map((candidate) => {
              const evaluation = asRecord(candidate.evaluation);
              const source = asRecord(evaluation.source);
              const pairs = Array.isArray(source.pairs)
                ? source.pairs.map(asRecord)
                : [];
              const history = Array.isArray(candidate.history)
                ? candidate.history.map(asRecord)
                : [];
              return (
                <Card
                  size="small"
                  key={String(candidate.id)}
                  className={styles.candidate}
                  title={display(asRecord(candidate.proposal).summary)}
                  extra={<Tag>{tr(`statuses.${candidate.status}`)}</Tag>}
                >
                  <Descriptions
                    size="small"
                    column={1}
                    items={[
                      {
                        key: "id",
                        label: tr("fields.candidate"),
                        children: (
                          <Text code copyable>
                            {display(candidate.id)}
                          </Text>
                        ),
                      },
                      {
                        key: "baseline",
                        label: tr("fields.baselineRevision"),
                        children: (
                          <Text code className={styles.digest}>
                            {display(candidate.baseline_revision)}
                          </Text>
                        ),
                      },
                      {
                        key: "benchmark",
                        label: tr("fields.benchmarkRevision"),
                        children: display(candidate.benchmark_revision),
                      },
                      {
                        key: "gate",
                        label: tr("gate"),
                        children: evaluation.gate ? (
                          <Tag
                            color={evaluation.gate === "GO" ? "green" : "red"}
                          >
                            {display(evaluation.gate)}
                          </Tag>
                        ) : (
                          tr("notAssessed")
                        ),
                      },
                      {
                        key: "cases",
                        label: tr("caseCount"),
                        children: display(evaluation.case_count),
                      },
                      {
                        key: "improvement",
                        label: tr("improvement"),
                        children:
                          typeof evaluation.mean_improvement === "number"
                            ? evaluation.mean_improvement.toFixed(4)
                            : tr("unknown"),
                      },
                    ]}
                  />
                  {Array.isArray(evaluation.reasons) &&
                    evaluation.reasons.length > 0 && (
                      <Alert
                        type="warning"
                        message={evaluation.reasons.map(display).join(" · ")}
                      />
                    )}
                  <Collapse
                    items={[
                      {
                        key: "proposal",
                        label: tr("proposalDetails"),
                        children: (
                          <pre className={styles.json}>
                            {json(candidate.proposal)}
                          </pre>
                        ),
                      },
                      {
                        key: "cases",
                        label: tr("pairedCases"),
                        children: (
                          <Table
                            size="small"
                            rowKey={(row, index) => `${row.case_id}:${index}`}
                            dataSource={pairs}
                            pagination={{ pageSize: 5 }}
                            columns={[
                              {
                                title: tr("caseId"),
                                dataIndex: "case_id",
                                render: display,
                              },
                              {
                                title: tr("baselineScore"),
                                render: (_, row) =>
                                  display(asRecord(row.baseline).score),
                              },
                              {
                                title: tr("candidateScore"),
                                render: (_, row) =>
                                  display(asRecord(row.candidate).score),
                              },
                              {
                                title: tr("safety"),
                                render: (_, row) =>
                                  tr(
                                    asRecord(row.baseline).safety_passed ===
                                      true &&
                                      asRecord(row.candidate).safety_passed ===
                                        true
                                      ? "yes"
                                      : "no",
                                  ),
                              },
                            ]}
                            locale={{ emptyText: tr("notAssessed") }}
                          />
                        ),
                      },
                      {
                        key: "history",
                        label: tr("stateHistory"),
                        children: history.length ? (
                          <Timeline
                            items={history.map((entry) => ({
                              children: (
                                <>
                                  <Text>
                                    {entry.from
                                      ? `${tr(`statuses.${entry.from}`)} → `
                                      : ""}
                                    {tr(`statuses.${entry.to}`)}
                                  </Text>
                                  <br />
                                  <Text type="secondary">
                                    {typeof entry.at === "string"
                                      ? formatServerIsoDateTime(
                                          entry.at,
                                          timezone,
                                        )
                                      : "—"}
                                  </Text>
                                </>
                              ),
                            }))}
                          />
                        ) : (
                          <Text type="secondary">{tr("noHistory")}</Text>
                        ),
                      },
                    ]}
                  />
                </Card>
              );
            })
          )}
        </Card>
      )}
    </div>
  );
}
