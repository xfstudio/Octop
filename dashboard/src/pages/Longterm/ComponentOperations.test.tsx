import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { longtermApi, type LongtermTask } from "../../api/modules/longterm";
import en from "../../locales/componentOperations/en.json";
import ComponentOperations, { ComponentArtifacts } from "./ComponentOperations";

const mocks = vi.hoisted(() => ({ user: { id: 1 } as { id: number } | null }));
const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => mocks.user,
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
vi.mock("../../api/modules/longterm", () => ({
  longtermApi: { taskOperation: vi.fn() },
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => {
      const path = key.replace(/^componentOperations\./, "").split(".");
      let value: unknown = en;
      for (const part of path)
        value =
          value && typeof value === "object"
            ? (value as Record<string, unknown>)[part]
            : undefined;
      return typeof value === "string" ? value : key;
    },
  }),
}));

const task: LongtermTask = {
  task_id: "task-a",
  tenant_id: "tenant-a",
  space_id: "space-a",
  goal: "Check input",
  revision: 4,
  status: "completed",
  workflow: { nodes: [] },
};
const configured = {
  id: "any",
  name: "Component",
  status: "configured",
  detail: "connection_not_probed",
};

describe("component operation forms", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.user = { id: 1 };
    vi.mocked(longtermApi.taskOperation).mockResolvedValue({
      task: { ...task, revision: 5 },
      record: {},
    });
  });

  it("imports through a validated form, preserving CAS and unknown health", async () => {
    const user = userEvent.setup();
    const onSuccess = vi.fn();
    render(
      <ComponentOperations
        task={task}
        kind="evaluate"
        role="member"
        integration={{ ...configured, status: "available" }}
        onCancel={vi.fn()}
        onSuccess={onSuccess}
      />,
    );
    await user.type(
      screen.getByLabelText("Relative run directory"),
      "bm-smoke/task-1",
    );
    await user.type(screen.getByLabelText("Benchmark task ID"), "commerce-1");
    await user.click(
      screen.getByRole("button", { name: "Import actual results" }),
    );
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(longtermApi.taskOperation).toHaveBeenCalledWith(
      "tenant-a",
      "task-a",
      "evaluate",
      {
        expected_revision: 4,
        run_dir: "bm-smoke/task-1",
        benchmark_task_id: "commerce-1",
      },
    );
    expect(screen.queryByLabelText("Request JSON")).not.toBeInTheDocument();
  });

  it("rejects host paths before sending any request", async () => {
    const user = userEvent.setup();
    render(
      <ComponentOperations
        task={task}
        kind="evaluate"
        role="owner"
        integration={configured}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    await user.type(
      screen.getByLabelText("Relative run directory"),
      "C:/private/run",
    );
    await user.click(
      screen.getByRole("button", { name: "Import actual results" }),
    );
    await screen.findByText(en.validation.relativeRunDir);
    expect(longtermApi.taskOperation).not.toHaveBeenCalled();
  });

  it("disables unconfigured external operations and offers the management entry", async () => {
    const onManage = vi.fn();
    render(
      <ComponentOperations
        task={task}
        kind="provenance"
        role="owner"
        integration={{ ...configured, status: "unconfigured" }}
        onManageComponent={onManage}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Export verified evidence" }),
    ).toBeDisabled();
    await userEvent.click(
      screen.getByRole("button", { name: "Manage component" }),
    );
    expect(onManage).toHaveBeenCalledWith("semantica");
  });

  it("allows a viewer to recall but does not offer memory writes", async () => {
    const user = userEvent.setup();
    render(
      <ComponentOperations
        task={task}
        kind="memory"
        role="viewer"
        integration={configured}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    await user.type(screen.getByLabelText("Recall query"), "trusted source");
    await user.click(screen.getByRole("button", { name: "Recall memory" }));
    await waitFor(() =>
      expect(longtermApi.taskOperation).toHaveBeenCalledWith(
        "tenant-a",
        "task-a",
        "memory",
        { action: "recall", query: "trusted source", max_tokens: 2048 },
      ),
    );
    fireEvent.mouseDown(
      screen.getByRole("combobox", { name: "Memory operation" }),
    );
    const option = await screen.findByText("Delete memory document");
    expect(option.closest(".ant-select-item-option")).toHaveClass(
      "ant-select-item-option-disabled",
    );
  });

  it("keeps evolution admin-only even when the component is available", () => {
    render(
      <ComponentOperations
        task={task}
        kind="evolution"
        role="member"
        integration={configured}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Create manual candidate" }),
    ).toBeDisabled();
    expect(screen.getByText(en.adminRequired)).toBeInTheDocument();
  });

  it("discards a slow response after switching tasks", async () => {
    let resolve!: (result: unknown) => void;
    vi.mocked(longtermApi.taskOperation).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const onSuccess = vi.fn();
    const user = userEvent.setup();
    const props = {
      kind: "evaluate" as const,
      role: "owner",
      integration: configured,
      onCancel: vi.fn(),
      onSuccess,
    };
    const view = render(<ComponentOperations {...props} task={task} />);
    await user.type(screen.getByLabelText("Relative run directory"), "run-a");
    await user.click(
      screen.getByRole("button", { name: "Import actual results" }),
    );
    await waitFor(() =>
      expect(longtermApi.taskOperation).toHaveBeenCalledTimes(1),
    );
    view.rerender(
      <ComponentOperations {...props} task={{ ...task, task_id: "task-b" }} />,
    );
    resolve({ task, record: { run_id: "old-run" } });
    await waitFor(() => expect(screen.getByText("task-b")).toBeInTheDocument());
    expect(onSuccess).not.toHaveBeenCalled();
    expect(screen.queryByText(en.recorded)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Relative run directory")).toHaveValue("");
  });

  it("requires consent before Semantica export", async () => {
    const user = userEvent.setup();
    render(
      <ComponentOperations
        task={task}
        kind="provenance"
        role="owner"
        integration={configured}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Export verified evidence" }),
    );
    expect(longtermApi.taskOperation).not.toHaveBeenCalled();
    await user.click(screen.getByRole("checkbox", { name: en.consentExport }));
    await user.click(
      screen.getByRole("button", { name: "Export verified evidence" }),
    );
    await waitFor(() =>
      expect(longtermApi.taskOperation).toHaveBeenCalledWith(
        "tenant-a",
        "task-a",
        "provenance",
        { consent: true },
      ),
    );
  });

  it("requires an explicit permanent-deletion confirmation and uses the server deletion contract", async () => {
    const user = userEvent.setup();
    vi.mocked(longtermApi.taskOperation).mockResolvedValue({
      deleted: true,
      document_id: "octop-current-task",
      bank_id: "private-bank",
    });
    render(
      <ComponentOperations
        task={task}
        kind="memory"
        role="owner"
        integration={configured}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    fireEvent.mouseDown(
      screen.getByRole("combobox", { name: "Memory operation" }),
    );
    await user.click(await screen.findByText("Delete memory document"));
    await user.type(screen.getByLabelText("Document ID"), "octop-current-task");
    await user.click(
      screen.getByRole("button", { name: "Delete memory document" }),
    );
    expect(longtermApi.taskOperation).not.toHaveBeenCalled();
    await user.click(screen.getByRole("checkbox", { name: en.consentDelete }));
    await user.click(
      screen.getByRole("button", { name: "Delete memory document" }),
    );
    await waitFor(() =>
      expect(longtermApi.taskOperation).toHaveBeenCalledWith(
        "tenant-a",
        "task-a",
        "memory",
        { action: "delete", document_id: "octop-current-task", consent: true },
      ),
    );
    expect(await screen.findByText("Deleted")).toBeInTheDocument();
  });

  it("can create a manual candidate without a configured RNA model", async () => {
    const user = userEvent.setup();
    render(
      <ComponentOperations
        task={task}
        kind="evolution"
        role="owner"
        integration={{ ...configured, status: "unconfigured" }}
        onCancel={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );
    await user.type(
      screen.getByLabelText("Fixed benchmark revision"),
      "gold-v1",
    );
    await user.type(
      screen.getByLabelText("Improvement summary"),
      "Check missing input",
    );
    await user.type(
      screen.getByLabelText("Improvement hypothesis"),
      "Reduce omissions",
    );
    await user.click(
      screen.getByRole("button", { name: "Create manual candidate" }),
    );
    await waitFor(() =>
      expect(longtermApi.taskOperation).toHaveBeenCalledWith(
        "tenant-a",
        "task-a",
        "evolution",
        {
          action: "propose",
          expected_revision: 4,
          benchmark_revision: "gold-v1",
          proposal: {
            summary: "Check missing input",
            hypothesis: "Reduce omissions",
            strategy: "Check input",
          },
        },
      ),
    );
  });

  it("does not treat a blocked RNA generation as a successful candidate", async () => {
    const user = userEvent.setup();
    const onSuccess = vi.fn();
    vi.mocked(longtermApi.taskOperation).mockResolvedValue({
      source: "rna_dev",
      status: "blocked",
      reason: "configured_model_credentials_required",
    });
    render(
      <ComponentOperations
        task={task}
        kind="evolution"
        role="owner"
        integration={configured}
        onCancel={vi.fn()}
        onSuccess={onSuccess}
      />,
    );
    fireEvent.mouseDown(
      screen.getByRole("combobox", { name: "Candidate operation" }),
    );
    await user.click(await screen.findByText("Generate RNA candidate"));
    await user.type(
      screen.getByLabelText("Fixed benchmark revision"),
      "gold-v1",
    );
    await user.click(screen.getByRole("checkbox", { name: en.confirmRna }));
    await user.click(
      screen.getByRole("button", { name: "Generate RNA candidate" }),
    );
    await screen.findByText(en.blocked);
    expect(onSuccess).not.toHaveBeenCalled();
    expect(screen.queryByText(en.recorded)).not.toBeInTheDocument();
  });

  it("discards a pending result when the signed-in user changes", async () => {
    let resolve!: (result: unknown) => void;
    vi.mocked(longtermApi.taskOperation).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const onSuccess = vi.fn();
    const props = {
      task,
      kind: "memory" as const,
      role: "owner",
      integration: configured,
      onCancel: vi.fn(),
      onSuccess,
    };
    const view = render(<ComponentOperations {...props} />);
    const user = userEvent.setup();
    await user.type(
      screen.getByLabelText("Recall query"),
      "private information",
    );
    await user.click(screen.getByRole("button", { name: "Recall memory" }));
    await waitFor(() =>
      expect(longtermApi.taskOperation).toHaveBeenCalledTimes(1),
    );
    mocks.user = { id: 2 };
    view.rerender(<ComponentOperations {...props} />);
    resolve({
      source: "hindsight",
      results: [{ document_id: "old-doc", text: "private response" }],
    });
    await waitFor(() =>
      expect(screen.getByLabelText("Recall query")).toHaveValue(""),
    );
    expect(onSuccess).not.toHaveBeenCalled();
    expect(screen.queryByText("private response")).not.toBeInTheDocument();
  });

  it("shows persisted benchmark outcomes independently of process health and deduplicates candidate snapshots", () => {
    render(
      <ComponentArtifacts
        task={{
          ...task,
          artifacts: {
            evaluation: [
              {
                run_id: "run-actual",
                task_id: "case-1",
                goal_outcome: "not_achieved",
                score: null,
                execution_health: { status: "healthy" },
              },
            ],
            evolution: [
              { id: "c1", status: "proposed", proposal: { summary: "old" } },
              {
                id: "c1",
                status: "rejected",
                proposal: { summary: "latest" },
                evaluation: {
                  gate: "NO_GO",
                  reasons: ["hard_gate_failed:authorization"],
                  case_count: 3,
                  mean_improvement: 0.3,
                },
              },
            ],
          },
        }}
      />,
    );
    expect(screen.getByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("latest")).toBeInTheDocument();
    expect(screen.queryByText("old")).not.toBeInTheDocument();
    expect(screen.getByText("NO_GO")).toBeInTheDocument();
    expect(
      screen.getByText("hard_gate_failed:authorization"),
    ).toBeInTheDocument();
  });
});
