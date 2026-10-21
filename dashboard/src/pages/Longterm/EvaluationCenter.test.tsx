import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  longtermEvaluationApi,
  type EvaluationRun,
  type EvaluationSuite,
} from "../../api/modules/longtermEvaluation";
import EvaluationCenter from "./EvaluationCenter";

const { translate } = vi.hoisted(() => ({ translate: (key: string) => key }));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: translate }),
}));
vi.mock("../../api/modules/longtermEvaluation", () => ({
  longtermEvaluationApi: {
    suites: vi.fn(),
    runs: vi.fn(),
    badcases: vi.fn(),
    start: vi.fn(),
    run: vi.fn(),
    attempt: vi.fn(),
    command: vi.fn(),
    saveSuite: vi.fn(),
    importRuns: vi.fn(),
    compare: vi.fn(),
    updateBadcase: vi.fn(),
    regression: vi.fn(),
  },
}));
const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);
const suite: EvaluationSuite = {
  suite_id: "builtin-contract-v1",
  name: "Contract checks",
  version: 1,
  revision: 0,
  split: "capability",
  digest: "suite-sha",
  cases: [{ case_id: "echo" }],
};
const run: EvaluationRun = {
  run_id: "run-1",
  status: "completed",
  revision: 3,
  suite,
  subject: { kind: "builtin", digest: "frozen-subject-sha" },
  source_class: "synthetic",
  harness: "octop-native-v1",
  repeats: 3,
  mode: "local",
  aggregate: { planned_attempts: 3, verified_attempts: 0, coverage: 0 },
  created_at: 100,
  updated_at: 120,
  attempts: [
    {
      attempt_id: "attempt-1",
      case_id: "echo",
      repeat_index: 0,
      status: "completed",
      revision: 1,
      result: { evaluation_outcome: "unverified" },
    },
  ],
};
const task = {
  task_id: "task-1",
  tenant_id: "t1",
  space_id: "s1",
  goal: "My source workflow",
  status: "completed",
  revision: 1,
  workflow: { nodes: [] },
};
type Props = Parameters<typeof EvaluationCenter>[0];
function view(overrides: Partial<Props> = {}) {
  return (
    <ConfigProvider theme={{ token: { motion: false } }}>
      <EvaluationCenter
        tenantId="t1"
        spaceId="s1"
        connected
        role="member"
        tasks={[
          task,
          {
            ...task,
            task_id: "private-task",
            space_id: "s2",
            goal: "Other space workflow",
          },
        ]}
        {...overrides}
      />
    </ConfigProvider>
  );
}
async function ready() {
  const button = screen.getByRole("button", { name: "evaluationCenter.start" });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

describe("long-running task evaluation center", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(longtermEvaluationApi.suites).mockResolvedValue({
      builtin: suite,
      suites: [],
    });
    vi.mocked(longtermEvaluationApi.runs).mockResolvedValue({ runs: [run] });
    vi.mocked(longtermEvaluationApi.badcases).mockResolvedValue({
      badcases: [],
    });
    vi.mocked(longtermEvaluationApi.start).mockResolvedValue(run);
    vi.mocked(longtermEvaluationApi.run).mockResolvedValue(run);
  });

  it("defaults to a model-free frozen suite and keeps unknown outcomes distinct from zero", async () => {
    const user = userEvent.setup();
    render(view());
    await user.click(await ready());
    expect(longtermEvaluationApi.start).toHaveBeenCalledWith(
      "t1",
      {
        space_id: "s1",
        suite_id: "builtin-contract-v1",
        suite_version: 1,
        subject: { kind: "builtin" },
        repeats: 3,
        mode: "local",
        max_total_tokens: 100000,
        max_parallel: 2,
        idempotency_key: expect.any(String),
      },
      expect.any(AbortSignal),
    );
    const drawer = within(await screen.findByRole("dialog"));
    expect(drawer.getByText("synthetic / octop-native-v1")).toBeInTheDocument();
    expect(
      drawer.getByText("evaluationCenter.metrics.pass_rate").closest("tr"),
    ).toHaveTextContent("—");
    expect(
      drawer.getByText("evaluationCenter.metrics.coverage").closest("tr"),
    ).toHaveTextContent("0.0%");
    expect(
      drawer.getByText("evaluationCenter.outcomeHint"),
    ).toBeInTheDocument();
  });

  it("requires explicit authorization for a current-space native workflow", async () => {
    const user = userEvent.setup();
    render(view());
    const start = await ready();
    await user.click(
      screen.getByRole("combobox", { name: "evaluationCenter.subject" }),
    );
    await user.click(screen.getByText("evaluationCenter.sourceTask"));
    expect(start).toBeDisabled();
    await user.click(
      screen.getByRole("combobox", { name: "evaluationCenter.task" }),
    );
    expect(screen.queryByText("Other space workflow")).not.toBeInTheDocument();
    await user.click(screen.getByText("My source workflow"));
    expect(start).toBeDisabled();
    expect(longtermEvaluationApi.start).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("checkbox", { name: "evaluationCenter.modelConfirm" }),
    );
    await user.click(start);
    expect(longtermEvaluationApi.start).toHaveBeenCalledWith(
      "t1",
      expect.objectContaining({
        subject: { kind: "source_task", task_id: "task-1" },
      }),
      expect.any(AbortSignal),
    );
  });

  it("allows a viewer to inspect results while preventing runs, imports and suite mutations", async () => {
    const user = userEvent.setup();
    render(view({ role: "viewer" }));
    await waitFor(() => expect(longtermEvaluationApi.runs).toHaveBeenCalled());
    expect(
      screen.getByRole("button", { name: "evaluationCenter.start" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("textbox", { name: "evaluationCenter.runDirs" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "evaluationCenter.import" }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("tab", { name: "evaluationCenter.suites" }),
    );
    expect(
      screen.getByRole("button", { name: "evaluationCenter.copySuite" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("textbox", { name: "evaluationCenter.casesJson" }),
    ).toBeDisabled();
    expect(longtermEvaluationApi.start).not.toHaveBeenCalled();
    expect(longtermEvaluationApi.saveSuite).not.toHaveBeenCalled();
  });

  it("loads the actual attempt trace before opening a native task or returning its result", async () => {
    const user = userEvent.setup();
    const onSession = vi.fn();
    vi.mocked(longtermEvaluationApi.attempt).mockResolvedValue({
      ...run.attempts![0],
      native_task_id: "executed-task",
      evidence_valid: true,
      task: { task_id: "executed-task", status: "completed" },
      trace: [
        {
          node_id: "echo",
          operation: "local.echo",
          evidence_ref: "octop-evidence:executed-task:1:sha",
        },
      ],
    });
    render(view({ onSession }));
    await user.click(await ready());
    await user.click(
      await screen.findByRole("button", { name: "evaluationCenter.viewTrace" }),
    );
    expect(longtermEvaluationApi.attempt).toHaveBeenCalledWith(
      "t1",
      "run-1",
      "attempt-1",
      expect.any(AbortSignal),
    );
    const traceTitle = await screen.findByText(
      "evaluationCenter.attemptDetails",
    );
    const traceModal = within(
      traceTitle.closest('[role="dialog"]') as HTMLElement,
    );
    expect(
      traceModal.getByText(/octop-evidence:executed-task:1:sha/),
    ).toBeInTheDocument();
    await user.click(
      traceModal.getByRole("button", {
        name: "evaluationCenter.returnTaskResult",
      }),
    );
    expect(onSession).toHaveBeenCalledWith({
      component_id: "octop",
      operation: "task",
      task_id: "executed-task",
    });
  });

  it("does not let a human mark a badcase as verified", async () => {
    const user = userEvent.setup();
    vi.mocked(longtermEvaluationApi.badcases).mockResolvedValue({
      badcases: [
        {
          badcase_id: "bad-1",
          run_id: "run-1",
          attempt_id: "attempt-1",
          case_id: "echo",
          status: "open",
          revision: 2,
        },
      ],
    });
    render(view());
    await ready();
    await user.click(
      screen.getByRole("tab", { name: "evaluationCenter.badcases" }),
    );
    await user.click(
      screen.getByRole("button", { name: "evaluationCenter.triage" }),
    );
    await user.click(
      screen.getByRole("combobox", { name: "evaluationCenter.badcaseStatus" }),
    );
    expect(
      document.querySelectorAll('.ant-select-item-option[title="verified"]'),
    ).toHaveLength(0);
    expect(
      document.querySelectorAll('.ant-select-item-option[title="fixed"]'),
    ).toHaveLength(1);
    expect(
      screen.getByText("evaluationCenter.verifiedHint"),
    ).toBeInTheDocument();
    expect(longtermEvaluationApi.updateBadcase).not.toHaveBeenCalled();
  });

  it("hides evaluation portals when leaving the page and restores the selected run on return", async () => {
    const user = userEvent.setup();
    const rendered = render(view());
    await user.click(await ready());
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    rendered.rerender(view({ connected: false }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    rendered.rerender(view());
    expect(await screen.findByRole("dialog")).toHaveTextContent("run-1");
  });

  it("aborts and ignores evaluation responses after leaving the current scope", async () => {
    let resolve!: (value: EvaluationRun) => void;
    vi.mocked(longtermEvaluationApi.start).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const user = userEvent.setup();
    const rendered = render(view());
    await user.click(await ready());
    const signal = vi.mocked(longtermEvaluationApi.start).mock.calls[0][2]!;
    rendered.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      resolve(run);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
