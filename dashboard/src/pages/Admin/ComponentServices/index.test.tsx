import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import {
  componentManagementApi,
  ComponentLifecycleError,
  type ComponentCatalog,
  type ComponentRuntime,
  type ManagedComponentId,
} from "../../../api/modules/componentManagement";
import ComponentServicesPage from ".";

const { session } = vi.hoisted(() => ({
  session: { user: { id: 1, role: "admin" } },
}));
const getComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (element) => getComputedStyle(element);

vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => session.user,
}));
vi.mock("../../../layouts/PageShell", () => ({
  default: ({
    children,
    actions,
  }: {
    children: ReactNode;
    actions: ReactNode;
  }) => (
    <div>
      {actions}
      {children}
    </div>
  ),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: { name?: string; defaultValue?: string }) =>
      options?.name ? `${key}:${options.name}` : options?.defaultValue ?? key,
  }),
}));
vi.mock("../../../api/modules/componentManagement", async (importOriginal) => {
  const original = await importOriginal<
    typeof import("../../../api/modules/componentManagement")
  >();
  return {
    ...original,
    componentManagementApi: {
      catalog: vi.fn(),
      runtime: vi.fn(),
      lifecycle: vi.fn(),
      configuration: vi.fn(),
      save: vi.fn(),
    },
  };
});

const IDS = [
  "octop",
  "semantica",
  "beatdesign",
  "commerceagentbench",
  "rna",
  "hindsight",
  "temporal",
] as const;

function runtime(
  id: ManagedComponentId,
  patch: Partial<ComponentRuntime> = {},
): ComponentRuntime {
  return {
    component_id: id,
    name: id,
    mode: "embedded",
    state: "enabled",
    enabled: true,
    revision: 4,
    can_start: false,
    can_stop: true,
    reason: "runtime_embedded",
    services: [],
    console: {
      url: "/workbench/platform?section=integrations",
      kind: "internal",
      label: "integration",
      available: true,
    },
    ...patch,
  };
}

function catalog(): ComponentCatalog {
  return {
    can_manage: true,
    scope: "server",
    components: IDS.map((id) => ({
      id,
      name: id,
      configured: true,
      healthy: null,
      status: "configured",
      detail: "not_checked",
      checked_at: null,
      fields: [],
      actions: [],
    })),
  };
}

function page() {
  return (
    <MemoryRouter>
      <ConfigProvider theme={{ token: { motion: false } }}>
        <ComponentServicesPage />
      </ConfigProvider>
    </MemoryRouter>
  );
}

function card(id: ManagedComponentId) {
  return within(
    document.querySelector(`[data-service-component="${id}"]`)! as HTMLElement,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("component services administration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    session.user = { id: 1, role: "admin" };
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: true,
    });
    vi.mocked(componentManagementApi.catalog).mockResolvedValue(catalog());
    vi.mocked(componentManagementApi.runtime).mockResolvedValue({
      components: IDS.map((id) => runtime(id)),
    });
    vi.mocked(componentManagementApi.lifecycle).mockResolvedValue(
      runtime("semantica", { state: "disabled", enabled: false }),
    );
  });

  it("does not fetch server administration data for ordinary users", () => {
    session.user = { id: 2, role: "user" };
    render(page());
    expect(
      screen.getByText("componentCenter.services.adminOnly"),
    ).toBeInTheDocument();
    expect(componentManagementApi.catalog).not.toHaveBeenCalled();
    expect(componentManagementApi.runtime).not.toHaveBeenCalled();
  });

  it("keeps offline lifecycle controls locked without sending requests", () => {
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: false,
    });
    render(page());
    expect(
      screen.getByText("componentCenter.services.offline"),
    ).toBeInTheDocument();
    expect(componentManagementApi.runtime).not.toHaveBeenCalled();
    expect(
      card("semantica").getByRole("button", {
        name: "Semantica componentCenter.services.stop",
      }),
    ).toBeDisabled();
  });

  it("distinguishes managed processes, integrations, external capabilities and console types", async () => {
    vi.mocked(componentManagementApi.runtime).mockResolvedValue({
      components: IDS.map((id) =>
        runtime(
          id,
          id === "temporal"
            ? {
                mode: "compose",
                state: "running",
                reason: null,
                services: [
                  { name: "temporal", state: "running", health: "healthy" },
                ],
                console: {
                  url: "http://localhost:8233/",
                  kind: "external",
                  label: "native",
                  available: true,
                },
              }
            : id === "rna"
            ? {
                mode: "process",
                state: "stopped",
                can_start: true,
                can_stop: false,
              }
            : id === "hindsight"
            ? {
                mode: "external",
                state: "unknown",
                can_start: false,
                can_stop: false,
                reason: "runtime_external",
              }
            : {},
        ),
      ),
    });
    render(page());
    expect(
      await card("semantica").findByRole("button", {
        name: "Semantica componentCenter.services.disable",
      }),
    ).toBeEnabled();
    expect(
      card("rna").getByRole("button", {
        name: "RNA componentCenter.services.start",
      }),
    ).toBeEnabled();
    expect(
      card("rna").getByRole("button", {
        name: "RNA componentCenter.services.stop",
      }),
    ).toBeDisabled();
    expect(
      card("hindsight").getByRole("button", {
        name: "Hindsight componentCenter.services.disable",
      }),
    ).toBeDisabled();
    expect(document.querySelectorAll("[data-service-component]")).toHaveLength(
      7,
    );
    const nativeLink = card("temporal").getByRole("link", {
      name: "componentCenter.services.nativeConsole",
    });
    expect(nativeLink).toHaveAttribute("target", "_blank");
    expect(nativeLink).toHaveAttribute("rel", "noopener noreferrer");
    expect(
      card("semantica").getByRole("link", {
        name: "componentCenter.services.integrationConsole",
      }),
    ).toHaveAttribute("href", "/workbench/platform?section=integrations");
    expect(
      screen.getByRole("link", { name: "componentCenter.services.workbench" }),
    ).toHaveAttribute("href", "/workbench/platform?section=integrations");
    expect(
      card("temporal").getByRole("list", {
        name: "componentCenter.services.serviceList",
      }),
    ).toBeInTheDocument();
  });

  it("requires explicit confirmation and sends only one action while pending, then reads actual state", async () => {
    const user = userEvent.setup();
    const pending = deferred<ComponentRuntime>();
    vi.mocked(componentManagementApi.lifecycle).mockReturnValue(
      pending.promise,
    );
    render(page());
    const stop = await card("semantica").findByRole("button", {
      name: "Semantica componentCenter.services.disable",
    });
    await user.click(stop);
    expect(
      screen.getByText("componentCenter.services.stopWarning"),
    ).toBeInTheDocument();
    expect(componentManagementApi.lifecycle).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", {
      name: "componentCenter.services.confirmAction",
    });
    await user.dblClick(confirm);
    expect(componentManagementApi.lifecycle).toHaveBeenCalledExactlyOnceWith(
      "semantica",
      "stop",
      4,
      expect.any(AbortSignal),
    );
    expect(
      card("octop").getByRole("button", {
        name: "Octop componentCenter.services.disable",
      }),
    ).toBeDisabled();
    vi.mocked(componentManagementApi.runtime).mockResolvedValue({
      components: IDS.map((id) =>
        runtime(
          id,
          id === "semantica"
            ? {
                state: "disabled",
                enabled: false,
                revision: 5,
                can_start: true,
                can_stop: false,
              }
            : {},
        ),
      ),
    });
    await act(async () =>
      pending.resolve(runtime("semantica", { state: "disabled" })),
    );
    await waitFor(() =>
      expect(componentManagementApi.runtime).toHaveBeenCalledTimes(2),
    );
    expect(
      await card("semantica").findByRole("button", {
        name: "Semantica componentCenter.services.enable",
      }),
    ).toBeEnabled();
  });

  it("does not retry a revision conflict and refreshes state before allowing another action", async () => {
    const user = userEvent.setup();
    vi.mocked(componentManagementApi.lifecycle).mockRejectedValue(
      new ComponentLifecycleError(409, "runtime_revision_conflict"),
    );
    render(page());
    await user.click(
      await card("semantica").findByRole("button", {
        name: "Semantica componentCenter.services.disable",
      }),
    );
    await user.click(
      screen.getByRole("button", {
        name: "componentCenter.services.confirmAction",
      }),
    );
    expect(
      await screen.findByText("componentCenter.services.conflict"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(componentManagementApi.runtime).toHaveBeenCalledTimes(2),
    );
    expect(componentManagementApi.lifecycle).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByText("componentCenter.services.operationCompleted"),
    ).not.toBeInTheDocument();
  });

  it("reports a timed-out action without claiming success and locks controls when observation also fails", async () => {
    const user = userEvent.setup();
    vi.mocked(componentManagementApi.lifecycle).mockRejectedValue(
      new ComponentLifecycleError(504, "runtime_timeout"),
    );
    render(page());
    await user.click(
      await card("temporal").findByRole("button", {
        name: "Temporal componentCenter.services.disable",
      }),
    );
    vi.mocked(componentManagementApi.runtime).mockRejectedValue(
      new Error("Request failed: 503 Unavailable"),
    );
    await user.click(
      screen.getByRole("button", {
        name: "componentCenter.services.confirmAction",
      }),
    );
    expect(
      await screen.findByText("componentCenter.services.loadError"),
    ).toBeInTheDocument();
    expect(
      card("temporal").getByRole("button", {
        name: "Temporal componentCenter.services.stop",
      }),
    ).toBeDisabled();
    expect(
      screen.queryByText("componentCenter.services.operationCompleted"),
    ).not.toBeInTheDocument();
    expect(componentManagementApi.lifecycle).toHaveBeenCalledTimes(1);
  });

  it("aborts and ignores a previous actor's pending action", async () => {
    const user = userEvent.setup();
    const pending = deferred<ComponentRuntime>();
    vi.mocked(componentManagementApi.lifecycle).mockReturnValue(
      pending.promise,
    );
    const view = render(page());
    await user.click(
      await card("semantica").findByRole("button", {
        name: "Semantica componentCenter.services.disable",
      }),
    );
    await user.click(
      screen.getByRole("button", {
        name: "componentCenter.services.confirmAction",
      }),
    );
    const signal = vi.mocked(componentManagementApi.lifecycle).mock
      .calls[0][3]!;
    session.user = { id: 2, role: "admin" };
    view.rerender(page());
    await waitFor(() =>
      expect(componentManagementApi.runtime).toHaveBeenCalledTimes(2),
    );
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(runtime("semantica")));
    expect(componentManagementApi.runtime).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByText("componentCenter.services.operationCompleted"),
    ).not.toBeInTheDocument();
  });

  it("locks management and closes pending dialogs when the server denies administration", async () => {
    const user = userEvent.setup();
    vi.mocked(componentManagementApi.lifecycle).mockRejectedValue(
      new ComponentLifecycleError(403, "runtime_permission_revoked"),
    );
    render(page());
    await user.click(
      await card("semantica").findByRole("button", {
        name: "Semantica componentCenter.services.disable",
      }),
    );
    await user.click(
      screen.getByRole("button", {
        name: "componentCenter.services.confirmAction",
      }),
    );
    expect(
      await screen.findByText("componentCenter.services.permissionDenied"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      card("semantica").getByRole("button", {
        name: "componentCenter.configurationTitle:Semantica",
      }),
    ).toBeDisabled();
    expect(card("semantica").queryByRole("link")).not.toBeInTheDocument();
  });

  it.each([
    "javascript:alert(1)",
    "https://user:password@example.test/",
    "https://example.test/?token=secret",
    "https://example.test/#secret",
    "/relative-console",
  ])("does not open an unsafe external console %s", async (url) => {
    vi.mocked(componentManagementApi.runtime).mockResolvedValue({
      components: [
        runtime("temporal", {
          console: { url, kind: "external", label: "native", available: true },
        }),
      ],
    });
    render(page());
    await waitFor(() =>
      expect(
        card("temporal").getByRole("button", {
          name: "Temporal componentCenter.services.disable",
        }),
      ).toBeEnabled(),
    );
    expect(card("temporal").queryByRole("link")).not.toBeInTheDocument();
  });
});
