import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { longtermApi, type LongtermTask } from "../../api/modules/longterm";
import {
  LOCAL_WORKSPACES_KEY,
  createLocalWorkspaceState,
  newLocalWorkspace,
} from "./localWorkspace";
import LongtermPage from ".";

const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);

const mocks = vi.hoisted(() => ({
  user: null as { id: number } | null,
  translate: (key: string) => key,
  timezone: vi.fn(() => "UTC"),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: mocks.translate }),
}));
vi.mock("../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => mocks.user,
}));
vi.mock("../../hooks/useServerTimezone", () => ({
  useServerTimezone: mocks.timezone,
}));
vi.mock("../../layouts/PageShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("../../utils/antdMessage", () => ({
  message: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("../../api/modules/longterm", () => ({
  longtermApi: {
    tenants: vi.fn(),
    integrations: vi.fn(),
    spaces: vi.fn(),
    tasks: vi.fn(),
    uploadConfiguration: vi.fn(),
    configuration: vi.fn(),
    command: vi.fn(),
    task: vi.fn(),
    evidence: vi.fn(),
    taskOperation: vi.fn(),
    importBeatDesign: vi.fn(),
  },
}));

describe("offline workbench and revision-safe sync", () => {
  beforeEach(() => {
    localStorage.clear();
    mocks.user = null;
    vi.clearAllMocks();
    vi.mocked(longtermApi.tenants).mockResolvedValue({
      tenants: [{ tenant_id: "t1", name: "Team", role: "owner", revision: 1 }],
    });
    vi.mocked(longtermApi.spaces).mockResolvedValue({
      spaces: [{ space_id: "s1", name: "Research", revision: 2 }],
    });
    vi.mocked(longtermApi.tasks).mockResolvedValue({ tasks: [] });
    vi.mocked(longtermApi.integrations).mockResolvedValue({ integrations: [] });
  });

  it("edits and persists device configuration without calling any server APIs", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <LongtermPage standalone />
      </MemoryRouter>,
    );
    const name = screen.getByRole("textbox", {
      name: "longterm.workspaceName",
    });
    await user.clear(name);
    await user.type(name, "Offline research");
    await waitFor(() =>
      expect(
        JSON.parse(localStorage.getItem(LOCAL_WORKSPACES_KEY)!).workspaces[0]
          .config.workspace_name,
      ).toBe("Offline research"),
    );
    expect(longtermApi.tenants).not.toHaveBeenCalled();
    expect(longtermApi.integrations).not.toHaveBeenCalled();
    expect(mocks.timezone).toHaveBeenCalledWith(false);
  });

  it("hides portal dialogs while the kept-alive workbench is not visible and preserves the draft", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <MemoryRouter>
        <LongtermPage standalone />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: "longterm.newLocal" }));
    await user.type(
      screen.getByRole("textbox", { name: "longterm.name" }),
      "Pending draft",
    );
    rerender(
      <MemoryRouter>
        <LongtermPage standalone isVisible={false} />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    rerender(
      <MemoryRouter>
        <LongtermPage standalone />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("textbox", { name: "longterm.name" }),
    ).toHaveValue("Pending draft");
  });

  it("preserves the device draft after a conflict and downloads a separate copy", async () => {
    mocks.user = { id: 1 };
    const saved = createLocalWorkspaceState("Device version");
    saved.workspaces[0].sync = {
      tenant_id: "t1",
      space_id: "s1",
      revision: 1,
      synced_at: new Date().toISOString(),
    };
    localStorage.setItem(LOCAL_WORKSPACES_KEY, JSON.stringify(saved));
    vi.mocked(longtermApi.uploadConfiguration).mockRejectedValue(
      new Error("Request failed: 409 Conflict"),
    );
    const remote = newLocalWorkspace("Server version").config;
    vi.mocked(longtermApi.configuration).mockResolvedValue({
      space_id: "s1",
      revision: 2,
      config: remote,
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <LongtermPage standalone />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("tab", { name: "longterm.syncTab" }));
    await user.click(
      screen.getByRole("combobox", { name: "longterm.selectTenant" }),
    );
    await user.click(await screen.findByText("Team (owner)"));
    await user.click(
      screen.getByRole("combobox", { name: "longterm.selectSpace" }),
    );
    await user.click(await screen.findByText("Research"));
    await user.click(screen.getByRole("button", { name: "longterm.upload" }));
    await screen.findByText("longterm.syncConflict");
    expect(longtermApi.uploadConfiguration).toHaveBeenCalledWith(
      "t1",
      "s1",
      expect.objectContaining({
        expected_revision: 1,
        device_id: saved.device_id,
      }),
    );
    let stored = JSON.parse(localStorage.getItem(LOCAL_WORKSPACES_KEY)!);
    expect(stored.workspaces).toHaveLength(1);
    expect(stored.workspaces[0].config.workspace_name).toBe("Device version");
    expect(stored.workspaces[0].sync.revision).toBe(1);
    await user.click(screen.getByRole("button", { name: "longterm.download" }));
    await waitFor(() =>
      expect(
        JSON.parse(localStorage.getItem(LOCAL_WORKSPACES_KEY)!).workspaces,
      ).toHaveLength(2),
    );
    stored = JSON.parse(localStorage.getItem(LOCAL_WORKSPACES_KEY)!);
    expect(
      stored.workspaces.map(
        (item: { config: { workspace_name: string } }) =>
          item.config.workspace_name,
      ),
    ).toEqual(["Device version", "Server version"]);
    expect(stored.workspaces[1].sync.revision).toBe(2);
  });

  it("binds approval to a waiting node, its exact digest and the reviewed task revision", async () => {
    mocks.user = { id: 1 };
    const task: LongtermTask = {
      task_id: "task-1",
      tenant_id: "t1",
      space_id: "s1",
      goal: "Approve the report write",
      status: "waiting_approval",
      revision: 3,
      plan_digest: "a".repeat(64),
      workflow: {
        nodes: [
          {
            id: "write-report",
            agent_id: "agent-1",
            risk: "R1",
            depends_on: [],
            input: { target: "report" },
          },
        ],
      },
      checkpoint: {
        waiting_nodes: ["write-report"],
        plan_digest: "a".repeat(64),
        cycle: 1,
      },
    };
    vi.mocked(longtermApi.tasks).mockResolvedValue({ tasks: [task] });
    vi.mocked(longtermApi.command).mockResolvedValue({
      ...task,
      status: "queued",
      revision: 4,
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <LongtermPage standalone />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("tab", { name: "longterm.syncTab" }));
    await user.click(
      screen.getByRole("combobox", { name: "longterm.selectTenant" }),
    );
    await user.click(await screen.findByText("Team (owner)"));
    await user.click(screen.getByRole("tab", { name: "longterm.tasksTab" }));
    await user.click(
      await screen.findByRole("button", { name: "longterm.review" }),
    );
    const approve = screen.getByRole("button", { name: "longterm.approve" });
    expect(approve).toBeDisabled();
    await user.click(
      screen.getByRole("checkbox", { name: "longterm.approvalConfirm" }),
    );
    await user.click(approve);
    await waitFor(() => expect(longtermApi.command).toHaveBeenCalledTimes(1));
    expect(longtermApi.command).toHaveBeenCalledWith("t1", "task-1", {
      command: "approve",
      expected_revision: 3,
      plan_digest: "a".repeat(64),
      node_id: "write-report",
    });
  });

  it("previews a real Canvas export offline and applies it only after confirmation", async () => {
    const user = userEvent.setup();
    const canvas = {
      document: {
        cards: [{ id: "card-a", prompt: "Generation prompt" }],
        edges: [],
      },
      octop_execution: {
        schema_version: 1,
        max_cycles: 3,
        nodes: [
          {
            card_id: "card-a",
            id: "research",
            agent_id: "agent-a",
            skill: "agent.analyze",
            risk: "R0",
            depends_on: [],
          },
        ],
      },
    };
    render(
      <MemoryRouter>
        <LongtermPage standalone />
      </MemoryRouter>,
    );
    await user.click(
      screen.getByRole("button", { name: "longterm.beatdesignImport" }),
    );
    const apply = screen.getByRole("button", {
      name: "longterm.beatdesignApply",
    });
    expect(apply).toBeDisabled();
    await user.click(
      screen.getByRole("textbox", { name: "longterm.beatdesignDocument" }),
    );
    await user.paste(JSON.stringify(canvas));
    await user.click(
      screen.getByRole("button", { name: "longterm.beatdesignLocalPreview" }),
    );
    await waitFor(() => expect(apply).toBeEnabled());
    expect(
      JSON.parse(localStorage.getItem(LOCAL_WORKSPACES_KEY)!).workspaces[0]
        .config.workflow.nodes,
    ).toHaveLength(0);
    await user.click(apply);
    await waitFor(() =>
      expect(
        JSON.parse(localStorage.getItem(LOCAL_WORKSPACES_KEY)!).workspaces[0]
          .config.workflow.nodes,
      ).toHaveLength(1),
    );
    const stored = JSON.parse(localStorage.getItem(LOCAL_WORKSPACES_KEY)!);
    expect(
      stored.workspaces[0].config.workflow.nodes[0].beatdesign_card_id,
    ).toBe("card-a");
    expect(stored.workspaces[0].config.defaults.max_cycles).toBe(3);
    expect(longtermApi.importBeatDesign).not.toHaveBeenCalled();
  });
});
