import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";
import {
  longtermEvaluationApi,
  type EvaluationAttempt,
  type EvaluationBadcase,
  type EvaluationComparison,
  type EvaluationRun,
  type EvaluationSuite,
} from "../../api/modules/longtermEvaluation";
import type { LongtermTask } from "../../api/modules/longterm";
import { apiErrorMessage } from "../../utils/apiError";
import { asRecord } from "./componentOperationsHelpers";
import { localIdentifier } from "./localWorkspace";
import {
  EXAMPLE_EVALUATION_CASES,
  evaluationMetric,
  parseBenchmarkRunDirs,
  parseEvaluationCases,
} from "./evaluationHelpers";
import styles from "./ComponentOperations.module.less";
import type { OperationSessionSeed } from "./OperationSessionDialog";

const json = (value: unknown) => JSON.stringify(value, null, 2);
const terminal = new Set(["completed", "cancelled", "failed"]);

export default function EvaluationCenter({
  tenantId,
  spaceId,
  role,
  connected,
  tasks,
  onOpenTask,
  onSession,
}: {
  tenantId: string;
  spaceId: string;
  role?: string;
  connected: boolean;
  tasks: LongtermTask[];
  onOpenTask?: (taskId: string) => void;
  onSession?: (seed: OperationSessionSeed) => void;
}) {
  const { t } = useTranslation();
  const tr = (key: string) => t(`evaluationCenter.${key}`);
  const [suites, setSuites] = useState<EvaluationSuite[]>([]);
  const [runs, setRuns] = useState<EvaluationRun[]>([]);
  const [badcases, setBadcases] = useState<EvaluationBadcase[]>([]);
  const [suiteId, setSuiteId] = useState("builtin-contract-v1");
  const [subject, setSubject] = useState<"builtin" | "source_task">("builtin");
  const [taskId, setTaskId] = useState("");
  const [modelConfirmed, setModelConfirmed] = useState(false);
  const [repeats, setRepeats] = useState(3);
  const [mode, setMode] = useState<"local" | "temporal">("local");
  const [tokens, setTokens] = useState(100000);
  const [parallel, setParallel] = useState(2);
  const [suiteName, setSuiteName] = useState("");
  const [split, setSplit] = useState<EvaluationSuite["split"]>("capability");
  const [suiteJson, setSuiteJson] = useState(json(EXAMPLE_EVALUATION_CASES));
  const [editingSuite, setEditingSuite] = useState<EvaluationSuite | null>(
    null,
  );
  const [baseline, setBaseline] = useState("");
  const [candidate, setCandidate] = useState("");
  const [improvement, setImprovement] = useState(0);
  const [comparison, setComparison] = useState<EvaluationComparison | null>(
    null,
  );
  const [runDirs, setRunDirs] = useState("");
  const [importConfirmed, setImportConfirmed] = useState(false);
  const [detail, setDetail] = useState<EvaluationRun | null>(null);
  const [attemptDetail, setAttemptDetail] = useState<EvaluationAttempt | null>(
    null,
  );
  const detailRef = useRef(detail);
  detailRef.current = detail;
  const [badcase, setBadcase] = useState<EvaluationBadcase | null>(null);
  const [badcaseStatus, setBadcaseStatus] = useState<"triaged" | "fixed">(
    "triaged",
  );
  const [rootCause, setRootCause] = useState("");
  const [fixRef, setFixRef] = useState("");
  const [verificationRun, setVerificationRun] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const active = useRef(true);
  const locked = useRef(false);
  const requests = useRef(new Set<AbortController>());
  const retryKeys = useRef(new Map<string, string>());
  const writer = ["owner", "admin", "member"].includes(role ?? "");
  const ready = connected && !!tenantId && !!spaceId;
  const currentSuite = suites.find((suite) => suite.suite_id === suiteId);
  const scopeTasks = tasks.filter(
    (task) => task.tenant_id === tenantId && task.space_id === spaceId,
  );
  const keyFor = (payload: unknown) => {
    const payloadKey = JSON.stringify(payload);
    if (!retryKeys.current.has(payloadKey))
      retryKeys.current.set(payloadKey, localIdentifier());
    return retryKeys.current.get(payloadKey)!;
  };

  const refresh = useCallback(async () => {
    if (!connected || !tenantId || !spaceId || !active.current) return;
    const controller = new AbortController();
    requests.current.add(controller);
    setLoading(true);
    try {
      const [suiteResult, runResult, badcaseResult] = await Promise.allSettled([
        longtermEvaluationApi.suites(tenantId, spaceId, controller.signal),
        longtermEvaluationApi.runs(tenantId, spaceId, controller.signal),
        longtermEvaluationApi.badcases(tenantId, spaceId, controller.signal),
      ]);
      if (!active.current || controller.signal.aborted) return;
      if (suiteResult.status === "fulfilled") {
        const values = [
          suiteResult.value.builtin,
          ...suiteResult.value.suites,
        ].filter((suite): suite is EvaluationSuite => !!suite);
        setSuites(
          values.filter(
            (suite, index) =>
              values.findIndex((item) => item.suite_id === suite.suite_id) ===
              index,
          ),
        );
      }
      if (runResult.status === "fulfilled") setRuns(runResult.value.runs);
      if (badcaseResult.status === "fulfilled")
        setBadcases(badcaseResult.value.badcases);
      if (
        [suiteResult, runResult, badcaseResult].some(
          (result) => result.status === "rejected",
        )
      )
        setError(t("evaluationCenter.loadFailed"));
      setLoaded(true);
      if (detailRef.current && !terminal.has(detailRef.current.status)) {
        const current = await longtermEvaluationApi.run(
          tenantId,
          detailRef.current.run_id,
          controller.signal,
        );
        if (active.current && !controller.signal.aborted) setDetail(current);
      }
    } catch (cause) {
      if (active.current && !controller.signal.aborted)
        setError(apiErrorMessage(cause, t("evaluationCenter.failed"), t));
    } finally {
      requests.current.delete(controller);
      if (active.current) setLoading(false);
    }
  }, [connected, tenantId, spaceId, t]);

  useEffect(() => {
    active.current = true;
    const controllers = requests.current;
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && !locked.current)
        void refresh();
    }, 5000);
    return () => {
      active.current = false;
      window.clearInterval(timer);
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
    };
  }, [refresh]);

  const perform = async (action: (signal: AbortSignal) => Promise<void>) => {
    if (!ready || locked.current) return;
    locked.current = true;
    const controller = new AbortController();
    requests.current.add(controller);
    setBusy(true);
    setError("");
    try {
      await action(controller.signal);
      if (active.current && !controller.signal.aborted) await refresh();
    } catch (cause) {
      if (active.current && !controller.signal.aborted)
        setError(
          cause instanceof Error &&
            ["paths", "cases", "credentials"].includes(cause.message)
            ? tr(`errors.${cause.message}`)
            : apiErrorMessage(cause, tr("failed"), t),
        );
    } finally {
      requests.current.delete(controller);
      locked.current = false;
      if (active.current) setBusy(false);
    }
  };
  const start = () =>
    perform(async (signal) => {
      if (
        !writer ||
        !currentSuite ||
        (subject === "source_task" && (!taskId || !modelConfirmed))
      )
        return;
      const body = {
        space_id: spaceId,
        suite_id: currentSuite.suite_id,
        suite_version: currentSuite.version,
        subject:
          subject === "builtin"
            ? { kind: "builtin" as const }
            : { kind: "source_task" as const, task_id: taskId },
        repeats,
        mode,
        max_total_tokens: tokens,
        max_parallel: parallel,
      };
      const run = await longtermEvaluationApi.start(
        tenantId,
        { ...body, idempotency_key: keyFor(body) },
        signal,
      );
      retryKeys.current.delete(JSON.stringify(body));
      if (active.current && !signal.aborted) {
        setDetail(run);
        setModelConfirmed(false);
      }
    });
  const command = (run: EvaluationRun, name: "pause" | "resume" | "cancel") =>
    perform(async (signal) => {
      if (!writer) return;
      const body = { command: name, expected_revision: run.revision };
      await longtermEvaluationApi.command(
        tenantId,
        run.run_id,
        { ...body, idempotency_key: keyFor({ run_id: run.run_id, ...body }) },
        signal,
      );
      const current = await longtermEvaluationApi.run(
        tenantId,
        run.run_id,
        signal,
      );
      if (active.current && !signal.aborted) setDetail(current);
    });
  const openRun = (id: string) =>
    perform(async (signal) => {
      const value = await longtermEvaluationApi.run(tenantId, id, signal);
      if (active.current && !signal.aborted) setDetail(value);
    });
  const openAttempt = (runId: string, attemptId: string) =>
    perform(async (signal) => {
      const value = await longtermEvaluationApi.attempt(
        tenantId,
        runId,
        attemptId,
        signal,
      );
      if (active.current && !signal.aborted) setAttemptDetail(value);
    });
  const runOptions = runs.map((run) => ({
    value: run.run_id,
    label: `${run.suite?.name ?? run.run_id} · ${run.run_id} · ${
      run.source_class
    } / ${run.harness}`,
  }));
  const metrics = (aggregate: Record<string, unknown>) => (
    <Descriptions
      size="small"
      column={{ xs: 1, sm: 2 }}
      items={[
        ...[
          "planned_attempts",
          "verified_attempts",
          "coverage",
          "pass_rate",
          "pass_rate_lower",
          "pass_rate_upper",
          "at_least_one_success_rate",
          "all_trials_success_rate",
        ].map((name) => ({
          key: name,
          label: tr(`metrics.${name}`),
          children: evaluationMetric(
            aggregate[name],
            !name.endsWith("attempts"),
          ),
        })),
        {
          key: "pass_at_k",
          label: tr("metrics.pass_at_k"),
          children:
            Object.entries(asRecord(aggregate.pass_at_k))
              .map(([k, value]) => `k=${k}: ${evaluationMetric(value, true)}`)
              .join(" · ") || "—",
        },
      ]}
    />
  );
  const openBadcase = (value: EvaluationBadcase) => {
    setBadcase(value);
    setBadcaseStatus(value.status === "fixed" ? "fixed" : "triaged");
    setRootCause(value.root_cause ?? "");
    setFixRef(value.fix_ref ?? "");
    setVerificationRun(value.verification_run_id ?? "");
  };

  return (
    <div className={styles.content}>
      <Space wrap>
        <Typography.Title level={4}>{tr("title")}</Typography.Title>
        <Button
          disabled={!ready || busy}
          loading={loading}
          onClick={() => {
            setError("");
            void refresh();
          }}
        >
          {tr("refresh")}
        </Button>
      </Space>
      <Alert
        type="info"
        showIcon
        message={tr("methodHint")}
        description={tr("scopeHint")}
      />
      {!ready && <Alert type="info" showIcon message={tr("selectScope")} />}
      {error && <Alert type="error" showIcon message={error} />}
      <Tabs
        items={[
          {
            key: "runs",
            label: tr("runs"),
            children: (
              <div className={styles.content}>
                <Card size="small" title={tr("startTitle")}>
                  <Form layout="vertical" disabled={!ready || !writer || busy}>
                    <Form.Item label={tr("suite")}>
                      <Select
                        aria-label={tr("suite")}
                        value={suiteId}
                        options={suites.map((suite) => ({
                          value: suite.suite_id,
                          label: `${suite.name} · v${suite.version} · ${tr(
                            `splits.${suite.split}`,
                          )}`,
                        }))}
                        onChange={setSuiteId}
                      />
                    </Form.Item>
                    <Form.Item label={tr("subject")}>
                      <Select
                        aria-label={tr("subject")}
                        value={subject}
                        options={[
                          { value: "builtin", label: tr("builtin") },
                          { value: "source_task", label: tr("sourceTask") },
                        ]}
                        onChange={(value: "builtin" | "source_task") => {
                          setSubject(value);
                          setModelConfirmed(false);
                        }}
                      />
                    </Form.Item>
                    {subject === "source_task" ? (
                      <>
                        <Alert
                          type="warning"
                          showIcon
                          message={tr("modelWarning")}
                        />
                        <Form.Item label={tr("task")}>
                          <Select
                            aria-label={tr("task")}
                            value={taskId || undefined}
                            options={scopeTasks.map((task) => ({
                              value: task.task_id,
                              label: task.goal,
                            }))}
                            onChange={(id: string) => {
                              setTaskId(id);
                              setModelConfirmed(false);
                            }}
                          />
                        </Form.Item>
                        <Checkbox
                          checked={modelConfirmed}
                          onChange={(event) =>
                            setModelConfirmed(event.target.checked)
                          }
                        >
                          {tr("modelConfirm")}
                        </Checkbox>
                      </>
                    ) : (
                      <Alert
                        type="success"
                        showIcon
                        message={tr("builtinHint")}
                      />
                    )}
                    <Space wrap>
                      <Form.Item label={tr("repeats")}>
                        <InputNumber
                          aria-label={tr("repeats")}
                          min={1}
                          max={5}
                          value={repeats}
                          onChange={(value) => setRepeats(value ?? 1)}
                        />
                      </Form.Item>
                      <Form.Item label={tr("tokenBudget")}>
                        <InputNumber
                          aria-label={tr("tokenBudget")}
                          min={1}
                          max={2000000}
                          value={tokens}
                          onChange={(value) => setTokens(value ?? 1)}
                        />
                      </Form.Item>
                      <Form.Item label={tr("parallel")}>
                        <InputNumber
                          aria-label={tr("parallel")}
                          min={1}
                          max={4}
                          value={parallel}
                          onChange={(value) => setParallel(value ?? 1)}
                        />
                      </Form.Item>
                      <Form.Item label={tr("mode")}>
                        <Select
                          aria-label={tr("mode")}
                          value={mode}
                          style={{ minWidth: 130 }}
                          options={[
                            { value: "local", label: "Local" },
                            { value: "temporal", label: "Temporal" },
                          ]}
                          onChange={setMode}
                        />
                      </Form.Item>
                    </Space>
                    <Button
                      type="primary"
                      disabled={
                        !loaded ||
                        !currentSuite ||
                        !ready ||
                        !writer ||
                        busy ||
                        (subject === "source_task" &&
                          (!taskId || !modelConfirmed))
                      }
                      loading={busy}
                      onClick={() => void start()}
                    >
                      {tr("start")}
                    </Button>
                  </Form>
                </Card>
                <Table
                  rowKey="run_id"
                  size="small"
                  dataSource={runs}
                  scroll={{ x: 750 }}
                  pagination={{ pageSize: 8 }}
                  columns={[
                    {
                      title: tr("runId"),
                      dataIndex: "run_id",
                      render: (id: string) => (
                        <Button
                          type="link"
                          disabled={busy || !ready}
                          onClick={() => void openRun(id)}
                        >
                          {id}
                        </Button>
                      ),
                    },
                    {
                      title: tr("suite"),
                      render: (_, run) =>
                        `${run.suite?.name ?? "—"} · v${
                          run.suite?.version ?? "—"
                        }`,
                    },
                    {
                      title: tr("status"),
                      dataIndex: "status",
                      render: (value: string) => <Tag>{value}</Tag>,
                    },
                    {
                      title: tr("source"),
                      render: (_, run) =>
                        `${run.source_class} / ${run.harness}`,
                    },
                    {
                      title: tr("metrics.coverage"),
                      render: (_, run) =>
                        evaluationMetric(run.aggregate?.coverage, true),
                    },
                    {
                      title: tr("metrics.pass_rate"),
                      render: (_, run) =>
                        evaluationMetric(run.aggregate?.pass_rate, true),
                    },
                  ]}
                  expandable={{
                    expandedRowRender: (run) => metrics(run.aggregate ?? {}),
                  }}
                />
                <Card size="small" title={tr("importTitle")}>
                  <Alert type="info" showIcon message={tr("importHint")} />
                  <Input.TextArea
                    aria-label={tr("runDirs")}
                    rows={3}
                    value={runDirs}
                    onChange={(event) => {
                      setRunDirs(event.target.value);
                      setImportConfirmed(false);
                    }}
                    disabled={!ready || !writer || busy}
                  />
                  <Checkbox
                    checked={importConfirmed}
                    disabled={!ready || !writer || busy}
                    onChange={(event) =>
                      setImportConfirmed(event.target.checked)
                    }
                  >
                    {tr("importConfirm")}
                  </Checkbox>
                  <Button
                    disabled={
                      !ready ||
                      !writer ||
                      busy ||
                      !importConfirmed ||
                      !runDirs.trim()
                    }
                    onClick={() =>
                      void perform(async (signal) => {
                        const body = {
                          space_id: spaceId,
                          run_dirs: parseBenchmarkRunDirs(runDirs),
                        };
                        const run = await longtermEvaluationApi.importRuns(
                          tenantId,
                          { ...body, idempotency_key: keyFor(body) },
                          signal,
                        );
                        retryKeys.current.delete(JSON.stringify(body));
                        if (active.current && !signal.aborted) {
                          setDetail(run);
                          setImportConfirmed(false);
                        }
                      })
                    }
                  >
                    {tr("import")}
                  </Button>
                </Card>
              </div>
            ),
          },
          {
            key: "suites",
            label: tr("suites"),
            children: (
              <div className={styles.content}>
                <Table
                  size="small"
                  rowKey="suite_id"
                  dataSource={suites}
                  columns={[
                    { title: tr("name"), dataIndex: "name" },
                    { title: tr("version"), dataIndex: "version" },
                    {
                      title: tr("split"),
                      dataIndex: "split",
                      render: (value: string) => tr(`splits.${value}`),
                    },
                    {
                      title: tr("cases"),
                      render: (_, suite) => suite.cases.length,
                    },
                    {
                      title: tr("actions"),
                      render: (_, suite) => (
                        <Button
                          disabled={busy || !writer || !ready}
                          onClick={() => {
                            setEditingSuite(
                              suite.suite_id === "builtin-contract-v1"
                                ? null
                                : suite,
                            );
                            setSuiteName(
                              suite.suite_id === "builtin-contract-v1"
                                ? `${suite.name} copy`
                                : suite.name,
                            );
                            setSplit(suite.split);
                            setSuiteJson(json(suite.cases));
                          }}
                        >
                          {tr(
                            suite.suite_id === "builtin-contract-v1"
                              ? "copySuite"
                              : "editSuite",
                          )}
                        </Button>
                      ),
                    },
                  ]}
                  expandable={{
                    expandedRowRender: (suite) => (
                      <pre className={styles.json}>
                        {json({ digest: suite.digest, cases: suite.cases })}
                      </pre>
                    ),
                  }}
                />
                <Card
                  size="small"
                  title={tr(editingSuite ? "newVersion" : "newSuite")}
                >
                  <Alert type="info" showIcon message={tr("suiteHint")} />
                  <Form layout="vertical" disabled={!ready || !writer || busy}>
                    <Form.Item label={tr("name")}>
                      <Input
                        aria-label={tr("name")}
                        value={suiteName}
                        onChange={(event) => setSuiteName(event.target.value)}
                        maxLength={120}
                      />
                    </Form.Item>
                    <Form.Item label={tr("split")}>
                      <Select
                        aria-label={tr("split")}
                        value={split}
                        options={[
                          {
                            value: "capability",
                            label: tr("splits.capability"),
                          },
                          {
                            value: "regression",
                            label: tr("splits.regression"),
                          },
                          {
                            value: "holdout",
                            label: tr("splits.holdout"),
                          },
                        ]}
                        onChange={setSplit}
                      />
                    </Form.Item>
                    <Form.Item label={tr("casesJson")}>
                      <Input.TextArea
                        aria-label={tr("casesJson")}
                        rows={14}
                        value={suiteJson}
                        onChange={(event) => setSuiteJson(event.target.value)}
                      />
                    </Form.Item>
                    <Space>
                      <Button
                        disabled={
                          !suiteName.trim() || !ready || !writer || busy
                        }
                        onClick={() =>
                          void perform(async (signal) => {
                            const suite = await longtermEvaluationApi.saveSuite(
                              tenantId,
                              {
                                space_id: spaceId,
                                name: suiteName.trim(),
                                split,
                                cases: parseEvaluationCases(suiteJson),
                                expected_revision: editingSuite?.revision ?? 0,
                                ...(editingSuite
                                  ? { suite_id: editingSuite.suite_id }
                                  : {}),
                              },
                              signal,
                            );
                            if (active.current && !signal.aborted) {
                              setEditingSuite(suite);
                              setSuiteId(suite.suite_id);
                            }
                          })
                        }
                      >
                        {tr("saveSuite")}
                      </Button>
                      <Button
                        onClick={() => {
                          setEditingSuite(null);
                          setSuiteName("");
                          setSuiteJson(json(EXAMPLE_EVALUATION_CASES));
                        }}
                      >
                        {tr("reset")}
                      </Button>
                    </Space>
                  </Form>
                </Card>
              </div>
            ),
          },
          {
            key: "compare",
            label: tr("compare"),
            children: (
              <Card size="small" title={tr("compare")}>
                <Alert type="info" showIcon message={tr("compareHint")} />
                <Form layout="vertical" disabled={!ready || !writer || busy}>
                  <Form.Item label={tr("baseline")}>
                    <Select
                      aria-label={tr("baseline")}
                      value={baseline || undefined}
                      options={runOptions}
                      onChange={(id: string) => {
                        setBaseline(id);
                        setComparison(null);
                      }}
                    />
                  </Form.Item>
                  <Form.Item label={tr("candidate")}>
                    <Select
                      aria-label={tr("candidate")}
                      value={candidate || undefined}
                      options={runOptions.filter(
                        (entry) => entry.value !== baseline,
                      )}
                      onChange={(id: string) => {
                        setCandidate(id);
                        setComparison(null);
                      }}
                    />
                  </Form.Item>
                  <Form.Item label={tr("improvement")}>
                    <InputNumber
                      aria-label={tr("improvement")}
                      value={improvement}
                      min={0}
                      max={1}
                      step={0.01}
                      onChange={(value) => {
                        setImprovement(value ?? 0);
                        setComparison(null);
                      }}
                    />
                  </Form.Item>
                  <Button
                    disabled={
                      !ready ||
                      !writer ||
                      busy ||
                      !baseline ||
                      !candidate ||
                      baseline === candidate
                    }
                    onClick={() =>
                      void perform(async (signal) => {
                        const value = await longtermEvaluationApi.compare(
                          tenantId,
                          {
                            baseline_run_id: baseline,
                            candidate_run_id: candidate,
                            min_improvement: improvement,
                          },
                          signal,
                        );
                        if (active.current && !signal.aborted)
                          setComparison(value);
                      })
                    }
                  >
                    {tr("compare")}
                  </Button>
                </Form>
                {comparison && (
                  <>
                    <Alert
                      type={comparison.gate === "GO" ? "success" : "warning"}
                      showIcon
                      message={`${comparison.gate} · ${tr(
                        comparison.release_eligible
                          ? "releaseEligible"
                          : "notReleaseEligible",
                      )}`}
                      description={comparison.reasons.map(String).join(" · ")}
                    />
                    <Descriptions
                      size="small"
                      items={[
                        {
                          key: "improvement",
                          label: tr("meanImprovement"),
                          children: evaluationMetric(
                            comparison.mean_improvement,
                            true,
                          ),
                        },
                      ]}
                    />
                    <pre className={styles.json}>{json(comparison)}</pre>
                  </>
                )}
              </Card>
            ),
          },
          {
            key: "badcases",
            label: tr("badcases"),
            children: (
              <>
                <Alert type="info" showIcon message={tr("badcaseHint")} />
                <Table
                  rowKey="badcase_id"
                  size="small"
                  dataSource={badcases}
                  columns={[
                    { title: tr("case"), dataIndex: "case_id" },
                    { title: tr("status"), dataIndex: "status" },
                    { title: tr("rootCause"), dataIndex: "root_cause" },
                    {
                      title: tr("actions"),
                      render: (_, value) => (
                        <Space>
                          <Button
                            disabled={
                              !ready ||
                              !writer ||
                              busy ||
                              value.status === "verified"
                            }
                            onClick={() => openBadcase(value)}
                          >
                            {tr("triage")}
                          </Button>
                          <Button
                            disabled={!ready || !writer || busy}
                            onClick={() =>
                              void perform(async (signal) => {
                                await longtermEvaluationApi.regression(
                                  tenantId,
                                  value.badcase_id,
                                  { expected_revision: value.revision },
                                  signal,
                                );
                              })
                            }
                          >
                            {tr("addRegression")}
                          </Button>
                          <Button
                            disabled={!ready || busy}
                            onClick={() => void openRun(value.run_id)}
                          >
                            {tr("viewRun")}
                          </Button>
                        </Space>
                      ),
                    },
                  ]}
                  expandable={{
                    expandedRowRender: (value) => (
                      <pre className={styles.json}>{json(value)}</pre>
                    ),
                  }}
                />
              </>
            ),
          },
        ]}
      />
      <Drawer
        open={ready && !!detail}
        title={tr("runDetails")}
        onClose={() => setDetail(null)}
        width="min(980px, 100vw)"
      >
        {detail && (
          <div className={styles.content}>
            <Descriptions
              size="small"
              column={1}
              items={[
                { key: "id", label: tr("runId"), children: detail.run_id },
                { key: "status", label: tr("status"), children: detail.status },
                {
                  key: "source",
                  label: tr("source"),
                  children: `${detail.source_class} / ${detail.harness}`,
                },
                {
                  key: "subject",
                  label: tr("subjectDigest"),
                  children: (
                    <Typography.Text code copyable>
                      {detail.subject?.digest ?? "—"}
                    </Typography.Text>
                  ),
                },
              ]}
            />
            <Space>
              <Button
                disabled={busy || !ready}
                onClick={() => void openRun(detail.run_id)}
              >
                {tr("refresh")}
              </Button>
              {!terminal.has(detail.status) && (
                <>
                  <Button
                    disabled={
                      !writer || busy || !ready || detail.status === "paused"
                    }
                    onClick={() => void command(detail, "pause")}
                  >
                    {tr("pause")}
                  </Button>
                  <Button
                    disabled={
                      !writer || busy || !ready || detail.status !== "paused"
                    }
                    onClick={() => void command(detail, "resume")}
                  >
                    {tr("resume")}
                  </Button>
                  <Button
                    danger
                    disabled={!writer || busy || !ready}
                    onClick={() => void command(detail, "cancel")}
                  >
                    {tr("cancel")}
                  </Button>
                </>
              )}
            </Space>
            {metrics(detail.aggregate ?? {})}
            <Alert type="info" showIcon message={tr("outcomeHint")} />
            <Table
              size="small"
              rowKey="attempt_id"
              dataSource={detail.attempts ?? []}
              scroll={{ x: 650 }}
              columns={[
                { title: tr("case"), dataIndex: "case_id" },
                { title: tr("repeat"), dataIndex: "repeat_index" },
                { title: tr("status"), dataIndex: "status" },
                {
                  title: tr("evaluationOutcome"),
                  render: (_, attempt) =>
                    String(attempt.result?.evaluation_outcome ?? "unverified"),
                },
                {
                  title: tr("score"),
                  render: (_, attempt) =>
                    evaluationMetric(attempt.result?.score),
                },
                {
                  title: tr("actions"),
                  render: (_, attempt) => (
                    <Button
                      disabled={!ready || busy}
                      onClick={() =>
                        void openAttempt(detail.run_id, attempt.attempt_id)
                      }
                    >
                      {tr("viewTrace")}
                    </Button>
                  ),
                },
              ]}
              expandable={{
                expandedRowRender: (attempt) => (
                  <pre className={styles.json}>{json(attempt)}</pre>
                ),
              }}
            />
          </div>
        )}
      </Drawer>
      <Modal
        open={ready && !!attemptDetail}
        title={tr("attemptDetails")}
        onCancel={() => setAttemptDetail(null)}
        footer={null}
        width={940}
      >
        {attemptDetail && (
          <div className={styles.content}>
            <Alert type="info" showIcon message={tr("traceHint")} />
            {attemptDetail.evidence_valid !== undefined && (
              <Tag color={attemptDetail.evidence_valid ? "green" : "orange"}>
                {tr(
                  attemptDetail.evidence_valid
                    ? "evidenceValid"
                    : "evidenceUnverified",
                )}
              </Tag>
            )}
            {attemptDetail.task && (
              <Space wrap>
                {onOpenTask && (
                  <Button
                    onClick={() => {
                      onOpenTask(attemptDetail.task!.task_id);
                      setAttemptDetail(null);
                      setDetail(null);
                    }}
                  >
                    {tr("openNativeTask")}
                  </Button>
                )}
                {onSession && (
                  <Button
                    onClick={() => {
                      onSession({
                        component_id: "octop",
                        operation: "task",
                        task_id: attemptDetail.task!.task_id,
                      });
                      setAttemptDetail(null);
                      setDetail(null);
                    }}
                  >
                    {tr("returnTaskResult")}
                  </Button>
                )}
              </Space>
            )}
            <pre className={styles.json}>{json(attemptDetail)}</pre>
          </div>
        )}
      </Modal>
      <Modal
        open={ready && !!badcase}
        title={tr("triage")}
        onCancel={() => setBadcase(null)}
        onOk={() =>
          void perform(async (signal) => {
            if (!badcase || !writer) return;
            await longtermEvaluationApi.updateBadcase(
              tenantId,
              badcase.badcase_id,
              {
                expected_revision: badcase.revision,
                status: badcaseStatus,
                root_cause: rootCause.trim(),
                fix_ref: fixRef.trim(),
                ...(verificationRun
                  ? { verification_run_id: verificationRun }
                  : {}),
              },
              signal,
            );
            if (active.current && !signal.aborted) setBadcase(null);
          })
        }
        okButtonProps={{
          disabled:
            !ready ||
            !writer ||
            busy ||
            !rootCause.trim() ||
            (badcaseStatus === "fixed" && !fixRef.trim()),
        }}
      >
        <Alert type="info" showIcon message={tr("verifiedHint")} />
        <Form layout="vertical" disabled={busy}>
          <Form.Item label={tr("status")}>
            <Select
              aria-label={tr("badcaseStatus")}
              value={badcaseStatus}
              options={[
                { value: "triaged", label: "triaged" },
                { value: "fixed", label: "fixed" },
              ]}
              onChange={setBadcaseStatus}
            />
          </Form.Item>
          <Form.Item label={tr("rootCause")}>
            <Input.TextArea
              aria-label={tr("rootCause")}
              value={rootCause}
              onChange={(event) => setRootCause(event.target.value)}
            />
          </Form.Item>
          <Form.Item label={tr("fixRef")}>
            <Input
              aria-label={tr("fixRef")}
              value={fixRef}
              onChange={(event) => setFixRef(event.target.value)}
            />
          </Form.Item>
          <Form.Item label={tr("verificationRun")}>
            <Select
              aria-label={tr("verificationRun")}
              value={verificationRun || undefined}
              allowClear
              options={runOptions}
              onChange={(id: string | undefined) =>
                setVerificationRun(id ?? "")
              }
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
