import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  componentManagementApi,
  type ComponentCatalog,
  type ComponentConfiguration,
  type ComponentConnectionCheck,
  type ComponentProjectRead,
} from "../../api/modules/componentManagement";
import type { LongtermTask } from "../../api/modules/longterm";
import ComponentCenter, { type ComponentCenterProps } from "./ComponentCenter";

const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: { name?: string; operation?: string }) =>
      options?.name ? `${key}:${options.name}` : key,
  }),
}));

vi.mock("../../api/modules/componentManagement", () => ({
  componentManagementApi: {
    catalog: vi.fn(),
    configuration: vi.fn(),
    save: vi.fn(),
    check: vi.fn(),
    project: vi.fn(),
    bindProject: vi.fn(),
    readProject: vi.fn(),
  },
}));

const IDS = [
  "octop",
  "semantica",
  "beatdesign",
  "commerceagentbench",
  "rna",
  "hindsight",
  "temporal",
] as const;

function catalog(canManage = true): ComponentCatalog {
  return {
    can_manage: canManage,
    scope: "server",
    components: IDS.map((id) => ({
      id,
      name: id,
      status: "configured",
      detail: "not_checked",
      configured: true,
      healthy: null,
      checked_at: null,
      fields: [],
      actions: [],
    })),
  };
}

const configuration: ComponentConfiguration = {
  component_id: "hindsight",
  values: {
    enabled: true,
    base_url: "https://memory.example.test",
    api_key: "server-secret-must-not-be-read",
  },
  secret_configured: { api_key: true },
  fields: [
    {
      key: "enabled",
      type: "boolean",
      secret: false,
      required: false,
      configured: true,
    },
    {
      key: "base_url",
      type: "string",
      secret: false,
      required: true,
      configured: true,
    },
    {
      key: "api_key",
      type: "string",
      secret: true,
      required: false,
      configured: true,
    },
  ],
};

function task(id: string, tenantId = "t1", spaceId = "s1"): LongtermTask {
  return {
    task_id: id,
    tenant_id: tenantId,
    space_id: spaceId,
    goal: `Goal ${id}`,
    status: "completed",
    revision: 4,
    workflow: { nodes: [] },
  };
}

function props(
  overrides: Partial<ComponentCenterProps> = {},
): ComponentCenterProps {
  return {
    connected: true,
    isAdmin: true,
    actorId: 1,
    tenantId: "t1",
    spaceId: "s1",
    tenantRole: "owner",
    tasks: [
      task("mine"),
      task("other-space", "t1", "s2"),
      task("other-tenant", "t2"),
    ],
    canAccessPath: () => true,
    onNavigate: vi.fn(),
    onOperation: vi.fn(),
    onChanged: vi.fn(),
    ...overrides,
  };
}

function view(parameters: ComponentCenterProps) {
  return (
    <MemoryRouter>
      <ConfigProvider theme={{ token: { motion: false } }}>
        <ComponentCenter {...parameters} />
      </ConfigProvider>
    </MemoryRouter>
  );
}

function card(id: (typeof IDS)[number]) {
  return within(
    document.querySelector(`[data-component-id="${id}"]`)! as HTMLElement,
  );
}

async function openHindsight(user: ReturnType<typeof userEvent.setup>) {
  const button = screen.getByRole("button", {
    name: "componentCenter.configurationTitle:Hindsight",
  });
  await waitFor(() => expect(button).toBeEnabled());
  await user.click(button);
  return screen.findByLabelText("componentCenter.fields.base_url");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("component management center", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(componentManagementApi.catalog).mockResolvedValue(catalog());
    vi.mocked(componentManagementApi.configuration).mockResolvedValue(
      configuration,
    );
    vi.mocked(componentManagementApi.save).mockResolvedValue(configuration);
    vi.mocked(componentManagementApi.project).mockResolvedValue({
      component_id: "beatdesign",
      tenant_id: "t1",
      project_id: "bound-project",
      revision: 7,
      can_manage: true,
    });
  });

  it("links system admins to component services while keeping tenant admins separate", async () => {
    const view = render(viewForAdmin());
    expect(
      await screen.findByRole("link", {
        name: "componentCenter.manageServices",
      }),
    ).toHaveAttribute("href", "/admin/components");
    await act(async () => view.rerender(viewForAdmin(false)));
    expect(
      screen.queryByRole("link", { name: "componentCenter.manageServices" }),
    ).not.toBeInTheDocument();
    function viewForAdmin(isAdmin = true) {
      return (
        <MemoryRouter>
          <ConfigProvider theme={{ token: { motion: false } }}>
            <ComponentCenter {...props({ isAdmin })} />
          </ConfigProvider>
        </MemoryRouter>
      );
    }
  });

  it("opens a scoped operation session from each component without requiring server administration", async () => {
    const user = userEvent.setup();
    const onSession = vi.fn();
    const parameters = props({
      isAdmin: false,
      tenantRole: "viewer",
      onSession,
    });
    const rendered = render(view(parameters));
    await screen.findByText("componentCenter.adminOnly");
    for (const id of IDS) {
      const entry = card(id).getByRole("button", {
        name: "operationSessions.entry",
      });
      await waitFor(() => expect(entry).toBeEnabled());
      await user.click(entry);
      expect(onSession).toHaveBeenLastCalledWith({ component_id: id });
    }
    rendered.rerender(view({ ...parameters, connected: false }));
    expect(
      screen.getAllByRole("button", { name: "operationSessions.entry" }),
    ).toHaveLength(7);
    for (const entry of screen.getAllByRole("button", {
      name: "operationSessions.entry",
    }))
      expect(entry).toBeDisabled();
  });

  it("keeps all component descriptions and device editing available offline without server requests", async () => {
    const user = userEvent.setup();
    const parameters = props({ connected: false });
    render(view(parameters));
    expect(document.querySelectorAll("[data-component-id]")).toHaveLength(7);
    await user.click(
      screen.getByRole("button", { name: "componentCenter.editOffline" }),
    );
    expect(parameters.onNavigate).toHaveBeenCalledWith("workflow");
    expect(componentManagementApi.catalog).not.toHaveBeenCalled();
    expect(componentManagementApi.configuration).not.toHaveBeenCalled();
    expect(
      card("semantica").getByRole("button", {
        name: "componentCenter.actions.provenance",
      }),
    ).toBeDisabled();
  });

  it("separates tenant administration from server administration and filters management links", async () => {
    vi.mocked(componentManagementApi.catalog).mockResolvedValue(catalog(false));
    render(
      view(
        props({
          isAdmin: false,
          tenantRole: "owner",
          canAccessPath: () => false,
        }),
      ),
    );
    await screen.findByText("componentCenter.adminOnly");
    expect(
      screen.getByRole("button", {
        name: "componentCenter.configurationTitle:Hindsight",
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Hindsight componentCenter.check" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("link", { name: "componentCenter.actions.models" }),
    ).not.toBeInTheDocument();
    expect(componentManagementApi.configuration).not.toHaveBeenCalled();
  });

  it("sends only changed configuration and never reads back or clears an existing secret implicitly", async () => {
    const user = userEvent.setup();
    const parameters = props();
    render(view(parameters));
    const url = await openHindsight(user);
    const secret = screen.getByLabelText("componentCenter.fields.api_key");
    expect(secret).toHaveValue("");
    expect(
      screen.queryByDisplayValue("server-secret-must-not-be-read"),
    ).not.toBeInTheDocument();
    await user.clear(url);
    await user.type(url, "https://new-memory.example.test");
    await user.click(
      screen.getByRole("button", { name: "componentCenter.save" }),
    );
    await waitFor(() =>
      expect(componentManagementApi.save).toHaveBeenCalledWith(
        "hindsight",
        {
          values: { base_url: "https://new-memory.example.test" },
          secrets: {},
          clear_secrets: [],
        },
        expect.any(AbortSignal),
      ),
    );
    expect(parameters.onChanged).toHaveBeenCalledTimes(1);
  });

  it("removes a secret only after the explicit clear choice", async () => {
    const user = userEvent.setup();
    render(view(props()));
    await openHindsight(user);
    await user.click(
      screen.getByRole("checkbox", {
        name: "componentCenter.fields.api_key componentCenter.clearSecret",
      }),
    );
    expect(
      screen.getByLabelText("componentCenter.fields.api_key"),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "componentCenter.save" }),
    );
    await waitFor(() =>
      expect(componentManagementApi.save).toHaveBeenCalledWith(
        "hindsight",
        { values: {}, secrets: {}, clear_secrets: ["api_key"] },
        expect.any(AbortSignal),
      ),
    );
  });

  it("replaces a credential only when a new value is entered", async () => {
    const user = userEvent.setup();
    render(view(props()));
    await openHindsight(user);
    await user.type(
      screen.getByLabelText("componentCenter.fields.api_key"),
      "new-device-test-credential",
    );
    await user.click(
      screen.getByRole("button", { name: "componentCenter.save" }),
    );
    await waitFor(() =>
      expect(componentManagementApi.save).toHaveBeenCalledWith(
        "hindsight",
        {
          values: {},
          secrets: { api_key: "new-device-test-credential" },
          clear_secrets: [],
        },
        expect.any(AbortSignal),
      ),
    );
  });

  it("keeps a disabled component's execution blocked while allowing its administrator to repair configuration", async () => {
    const user = userEvent.setup();
    const snapshot = catalog();
    snapshot.components.find(
      (component) => component.id === "semantica",
    )!.status = "disabled";
    vi.mocked(componentManagementApi.catalog).mockResolvedValue(snapshot);
    render(view(props()));
    const configure = screen.getByRole("button", {
      name: "componentCenter.configurationTitle:Semantica",
    });
    await waitFor(() => expect(configure).toBeEnabled());
    await user.click(
      screen.getByRole("combobox", { name: "componentCenter.selectTask" }),
    );
    await user.click(screen.getByText("Goal mine"));
    expect(
      card("semantica").getByRole("button", {
        name: "componentCenter.actions.provenance",
      }),
    ).toBeDisabled();
    expect(configure).toBeEnabled();
  });

  it("closes sensitive drafts immediately when administrator access is removed", async () => {
    const user = userEvent.setup();
    const parameters = props();
    const rendered = render(view(parameters));
    await openHindsight(user);
    await user.type(
      screen.getByLabelText("componentCenter.fields.api_key"),
      "new-private-secret",
    );
    rendered.rerender(view({ ...parameters, isAdmin: false }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      screen.queryByDisplayValue("new-private-secret"),
    ).not.toBeInTheDocument();
    expect(componentManagementApi.save).not.toHaveBeenCalled();
    await screen.findByText("componentCenter.adminOnly");
  });

  it("aborts and ignores slow checks after a space change", async () => {
    const user = userEvent.setup();
    const response = deferred<ComponentConnectionCheck>();
    vi.mocked(componentManagementApi.check).mockReturnValue(response.promise);
    const parameters = props();
    const rendered = render(view(parameters));
    const button = screen.getByRole("button", {
      name: "Hindsight componentCenter.check",
    });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    const signal = vi.mocked(componentManagementApi.check).mock.calls[0][1]!;
    rendered.rerender(view({ ...parameters, spaceId: "s2" }));
    expect(signal.aborted).toBe(true);
    await act(async () =>
      response.resolve({
        component_id: "hindsight",
        configured: true,
        healthy: true,
        checked_at: 1,
        checks: [{ code: "late-old-space", ok: true }],
      }),
    );
    expect(
      screen.queryByText("componentCenter.checks.late-old-space"),
    ).not.toBeInTheDocument();
    expect(parameters.onChanged).not.toHaveBeenCalled();
  });

  it("requires an explicit current-space task and clears the selection on scope change", async () => {
    const user = userEvent.setup();
    const parameters = props();
    const rendered = render(view(parameters));
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "componentCenter.configurationTitle:Hindsight",
        }),
      ).toBeEnabled(),
    );
    const operation = card("semantica").getByRole("button", {
      name: "componentCenter.actions.provenance",
    });
    expect(operation).toBeDisabled();
    await user.click(
      screen.getByRole("combobox", { name: "componentCenter.selectTask" }),
    );
    expect(screen.queryByText("Goal other-tenant")).not.toBeInTheDocument();
    expect(screen.queryByText("Goal other-space")).not.toBeInTheDocument();
    await user.click(screen.getByText("Goal mine"));
    await user.click(operation);
    expect(parameters.onOperation).toHaveBeenCalledWith(
      "provenance",
      parameters.tasks[0],
    );
    rendered.rerender(view({ ...parameters, spaceId: "s2" }));
    expect(
      card("semantica").getByRole("button", {
        name: "componentCenter.actions.provenance",
      }),
    ).toBeDisabled();
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "componentCenter.configurationTitle:Hindsight",
        }),
      ).toBeEnabled(),
    );
  });

  it("lets a viewer open memory recall while keeping task write operations disabled", async () => {
    const user = userEvent.setup();
    const parameters = props({ isAdmin: false, tenantRole: "viewer" });
    render(view(parameters));
    await screen.findByText("componentCenter.adminOnly");
    await user.click(
      screen.getByRole("combobox", { name: "componentCenter.selectTask" }),
    );
    await user.click(screen.getByText("Goal mine"));
    const memory = card("hindsight").getByRole("button", {
      name: "componentCenter.actions.memory",
    });
    expect(memory).toBeEnabled();
    expect(
      card("semantica").getByRole("button", {
        name: "componentCenter.actions.provenance",
      }),
    ).toBeDisabled();
    expect(
      card("rna").getByRole("button", {
        name: "componentCenter.actions.evolution",
      }),
    ).toBeDisabled();
    await user.click(memory);
    expect(parameters.onOperation).toHaveBeenCalledWith(
      "memory",
      parameters.tasks[0],
    );
  });

  it("locks configuration and checks after the server rejects changed permissions", async () => {
    const user = userEvent.setup();
    vi.mocked(componentManagementApi.check).mockRejectedValue(
      new Error("Request failed: 403 Forbidden"),
    );
    render(view(props()));
    const button = screen.getByRole("button", {
      name: "Hindsight componentCenter.check",
    });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    await screen.findByText("componentCenter.permissionDenied");
    expect(
      screen.getByRole("button", {
        name: "componentCenter.configurationTitle:Hindsight",
      }),
    ).toBeDisabled();
    expect(button).toBeDisabled();
  });

  it("reads only the tenant-bound project and hides binding writes from ordinary members", async () => {
    const user = userEvent.setup();
    vi.mocked(componentManagementApi.project).mockResolvedValue({
      component_id: "beatdesign",
      tenant_id: "t1",
      project_id: "bound-project",
      revision: 7,
      can_manage: false,
    });
    vi.mocked(componentManagementApi.readProject).mockResolvedValue({
      source: "beatdesign",
      operation: "project",
      project_id: "bound-project",
      result: { title: "Live project" },
    });
    render(view(props({ isAdmin: false, tenantRole: "member" })));
    await waitFor(() =>
      expect(
        card("beatdesign").getByRole("button", {
          name: "componentCenter.projectTools",
        }),
      ).toBeEnabled(),
    );
    await user.click(
      card("beatdesign").getByRole("button", {
        name: "componentCenter.projectTools",
      }),
    );
    await screen.findByDisplayValue("bound-project");
    expect(
      screen.getByRole("textbox", { name: "componentCenter.projectId" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "componentCenter.saveProject" }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "componentCenter.readProject" }),
    );
    await screen.findByText(/Live project/);
    expect(componentManagementApi.readProject).toHaveBeenCalledWith(
      "t1",
      "s1",
      "beatdesign",
      "project",
      expect.any(AbortSignal),
    );
    expect(componentManagementApi.bindProject).not.toHaveBeenCalled();
  });

  it("saves a project binding with the displayed revision and discards a read after a tenant switch", async () => {
    const user = userEvent.setup();
    const response = deferred<ComponentProjectRead>();
    vi.mocked(componentManagementApi.readProject).mockReturnValue(
      response.promise,
    );
    vi.mocked(componentManagementApi.bindProject).mockResolvedValue({
      component_id: "beatdesign",
      tenant_id: "t1",
      project_id: "new-project",
      revision: 8,
      can_manage: true,
    });
    const parameters = props();
    const rendered = render(view(parameters));
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "componentCenter.configurationTitle:BeatDesign",
        }),
      ).toBeEnabled(),
    );
    await user.click(
      card("beatdesign").getByRole("button", {
        name: "componentCenter.projectTools",
      }),
    );
    const input = await screen.findByDisplayValue("bound-project");
    await user.clear(input);
    await user.type(input, "new-project");
    await user.click(
      screen.getByRole("button", { name: "componentCenter.saveProject" }),
    );
    await screen.findByText("componentCenter.projectSaved");
    expect(componentManagementApi.bindProject).toHaveBeenCalledWith(
      "t1",
      "beatdesign",
      { project_id: "new-project", expected_revision: 7 },
      expect.any(AbortSignal),
    );
    await user.click(
      screen.getByRole("button", { name: "componentCenter.readProject" }),
    );
    const signal = vi.mocked(componentManagementApi.readProject).mock
      .calls[0][4]!;
    rendered.rerender(view({ ...parameters, tenantId: "t2" }));
    expect(signal.aborted).toBe(true);
    await act(async () =>
      response.resolve({
        source: "beatdesign",
        operation: "project",
        project_id: "new-project",
        result: { title: "Previous tenant private data" },
      }),
    );
    expect(
      screen.queryByText(/Previous tenant private data/),
    ).not.toBeInTheDocument();
  });
});
