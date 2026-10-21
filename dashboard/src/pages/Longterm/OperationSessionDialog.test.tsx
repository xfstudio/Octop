import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  operationSessionsApi,
  type OperationSession,
} from "../../api/modules/operationSessions";
import OperationSessionDialog from "./OperationSessionDialog";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));
vi.mock("../../api/modules/operationSessions", () => ({
  operationSessionsApi: {
    operations: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    action: vi.fn(),
  },
}));
const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);
const result = {
  source: "octop_task",
  component_id: "octop" as const,
  operation: "task",
  output: { answer: "recorded" },
  verified: true,
  usage: { total_tokens: 0 },
  draft_only: true as const,
  promotion_allowed: false as const,
};
const created: OperationSession = {
  session_id: "session-1",
  component_id: "octop",
  operation: "task",
  input: {},
  task_id: "task-1",
  node_id: null,
  snapshot: {
    console: {
      url: "http://127.0.0.1:3000",
      kind: "external",
      label: "native",
      available: true,
    },
  },
  status: "created",
  revision: 1,
  candidate: null,
  result: null,
  result_digest: null,
  source: null,
  created_at: 100,
  updated_at: 100,
};
const ready: OperationSession = {
  ...created,
  status: "ready",
  revision: 2,
  candidate: result,
  result_digest: "a".repeat(64),
  source: "octop_task",
};
const committed: OperationSession = {
  ...ready,
  status: "committed",
  revision: 3,
  result,
};
const task = {
  task_id: "task-1",
  tenant_id: "tenant-1",
  space_id: "space-1",
  goal: "Example task",
  status: "completed",
  revision: 4,
  workflow: { nodes: [] },
};
const draft = {
  id: "draft",
  skill: "local.echo",
  risk: "R0" as const,
  depends_on: [],
  input: { old: "input" },
};
function view(onApply = vi.fn(), initialResult?: unknown) {
  return (
    <MemoryRouter>
      <ConfigProvider theme={{ token: { motion: false } }}>
        <OperationSessionDialog
          tenantId="tenant-1"
          spaceId="space-1"
          seed={{
            component_id: "octop",
            operation: "task",
            task_id: "task-1",
            ...(initialResult === undefined
              ? {}
              : { initial_result: initialResult }),
          }}
          tasks={[task]}
          draftNodes={[draft]}
          connected
          onClose={vi.fn()}
          onApply={onApply}
        />
      </ConfigProvider>
    </MemoryRouter>
  );
}

describe("component operation sessions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(operationSessionsApi.operations).mockResolvedValue({
      operations: [
        {
          component_id: "octop",
          operation: "task",
          input_example: {},
          task_required: true,
        },
      ],
    });
    vi.mocked(operationSessionsApi.list).mockResolvedValue({ sessions: [] });
    vi.mocked(operationSessionsApi.create).mockResolvedValue(created);
    vi.mocked(operationSessionsApi.action).mockImplementation(
      async (_tenant, _space, _id, action) =>
        action === "commit" ? committed : ready,
    );
  });
  it("requires digest confirmation before returning a verified readback to the local draft", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    render(view(onApply));
    await user.click(
      screen.getByRole("button", { name: "operationSessions.create" }),
    );
    await screen.findByText("session-1");
    expect(operationSessionsApi.create).toHaveBeenCalledWith(
      "tenant-1",
      "space-1",
      expect.objectContaining({
        task_id: "task-1",
        component_id: "octop",
        operation: "task",
        input: {},
        idempotency_key: expect.any(String),
      }),
      expect.any(AbortSignal),
    );
    const consoleLink = screen.getByRole("link", {
      name: "operationSessions.openConsole",
    });
    expect(consoleLink).toHaveAttribute("target", "_blank");
    expect(consoleLink).toHaveAttribute("rel", "noopener noreferrer");
    await user.click(
      screen.getByRole("button", { name: "operationSessions.readback" }),
    );
    const save = await screen.findByRole("button", {
      name: "operationSessions.commit",
    });
    expect(save).toBeDisabled();
    expect(onApply).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("checkbox", { name: "operationSessions.commitConfirm" }),
    );
    await user.click(save);
    await user.click(
      await screen.findByRole("button", {
        name: "operationSessions.appendInput",
      }),
    );
    expect(operationSessionsApi.action).toHaveBeenCalledWith(
      "tenant-1",
      "space-1",
      "session-1",
      "commit",
      expect.objectContaining({
        expected_revision: 2,
        result_digest: "a".repeat(64),
      }),
      expect.any(AbortSignal),
    );
    expect(onApply).toHaveBeenCalledWith({ answer: "recorded" }, undefined);
    expect(draft.input).toEqual({ old: "input" });
  });

  it("compares existing draft input and requires explicit replacement confirmation", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn();
    vi.mocked(operationSessionsApi.create).mockResolvedValue(committed);
    render(view(onApply));
    await user.click(
      screen.getByRole("button", { name: "operationSessions.create" }),
    );
    await user.click(
      await screen.findByRole("combobox", { name: "operationSessions.target" }),
    );
    await user.click(
      await screen.findByText("draft", {
        selector: ".ant-select-item-option-content",
      }),
    );
    const replace = screen.getByRole("button", {
      name: "operationSessions.replaceInput",
    });
    expect(replace).toBeDisabled();
    expect(screen.getByText(/"old": "input"/)).toBeInTheDocument();
    await user.click(
      screen.getByRole("checkbox", {
        name: "operationSessions.replaceConfirm",
      }),
    );
    await user.click(replace);
    expect(onApply).toHaveBeenCalledWith({ answer: "recorded" }, "draft");
  });

  it("keeps manual imports explicitly unverified and blocks import until acknowledged", async () => {
    const user = userEvent.setup();
    vi.mocked(operationSessionsApi.action).mockResolvedValue({
      ...ready,
      candidate: { ...result, verified: false, source: "manual_unverified" },
    });
    render(view(vi.fn(), { fabricated: true }));
    await user.click(
      screen.getByRole("button", { name: "operationSessions.create" }),
    );
    const importButton = await screen.findByRole("button", {
      name: "operationSessions.import",
    });
    expect(importButton).toBeDisabled();
    await user.click(
      screen.getByRole("checkbox", { name: "operationSessions.importConfirm" }),
    );
    await user.click(importButton);
    await screen.findByText("operationSessions.manualUnverified");
    expect(operationSessionsApi.action).toHaveBeenCalledWith(
      "tenant-1",
      "space-1",
      "session-1",
      "import",
      expect.objectContaining({
        result: { fabricated: true },
        expected_revision: 1,
      }),
      expect.any(AbortSignal),
    );
  });

  it("does not claim success when the device draft rejects a result", async () => {
    const user = userEvent.setup();
    const onApply = vi.fn(() => false);
    vi.mocked(operationSessionsApi.create).mockResolvedValue(committed);
    render(view(onApply));
    await user.click(
      screen.getByRole("button", { name: "operationSessions.create" }),
    );
    const apply = await screen.findByRole("button", {
      name: "operationSessions.appendInput",
    });
    await user.click(apply);
    expect(onApply).toHaveBeenCalled();
    expect(
      screen.queryByText("operationSessions.applied"),
    ).not.toBeInTheDocument();
    expect(apply).toBeEnabled();
  });

  it("aborts an unfinished session request when its scope is unmounted", async () => {
    let resolve!: (value: OperationSession) => void;
    vi.mocked(operationSessionsApi.create).mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const user = userEvent.setup();
    const onApply = vi.fn();
    const rendered = render(view(onApply));
    await user.click(
      screen.getByRole("button", { name: "operationSessions.create" }),
    );
    await waitFor(() => expect(operationSessionsApi.create).toHaveBeenCalled());
    const signal = vi.mocked(operationSessionsApi.create).mock.calls[0][3]!;
    rendered.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      resolve(committed);
    });
    expect(onApply).not.toHaveBeenCalled();
  });
});
