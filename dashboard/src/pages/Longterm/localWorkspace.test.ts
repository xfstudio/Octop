import { beforeEach, describe, expect, it } from "vitest";
import {
  credentialPaths,
  createLocalWorkspaceState,
  isWorkspaceConfig,
  LOCAL_WORKSPACES_KEY,
  newLocalWorkspace,
  portableConfiguration,
  readLocalWorkspaceState,
  workflowProblem,
} from "./localWorkspace";
import type { WorkflowNode } from "../../api/modules/longterm";

const step = (id: string, depends_on: string[] = []): WorkflowNode => ({
  id,
  agent_id: "agent-1",
  risk: "R0",
  depends_on,
  input: { prompt: "Review the report" },
});

describe("longterm device configuration", () => {
  beforeEach(() => localStorage.clear());

  it("restores an offline draft without any server or authentication state", () => {
    const state = createLocalWorkspaceState("Offline work");
    state.workspaces[0].config.workflow.nodes = [
      step("research"),
      step("review", ["research"]),
    ];
    localStorage.setItem(LOCAL_WORKSPACES_KEY, JSON.stringify(state));
    expect(readLocalWorkspaceState("Unused default")).toEqual(state);
    expect(isWorkspaceConfig(state.workspaces[0].config)).toBe(true);
  });

  it("does not replace damaged existing data with a fresh empty draft", () => {
    localStorage.setItem(LOCAL_WORKSPACES_KEY, "{broken");
    expect(() => readLocalWorkspaceState("New draft")).toThrow();
    expect(localStorage.getItem(LOCAL_WORKSPACES_KEY)).toBe("{broken");
  });

  it("only exports portable configuration fields and reports nested credential paths", () => {
    const workspace = newLocalWorkspace("Portable");
    const config = {
      ...workspace.config,
      auth_token: "never-upload",
      credentials: { api_key: "device-only" },
    };
    config.workflow.nodes = [step("research")];
    config.workflow.nodes[0].input = {
      provider: { apiKey: "secret" },
      auth: { access_token: "secret" },
      max_tokens: 500,
      credential_ref: "runtime:provider",
    };
    const portable = portableConfiguration(config);
    expect(portable).not.toHaveProperty("auth_token");
    expect(portable).not.toHaveProperty("credentials");
    expect(credentialPaths(portable)).toEqual([
      "workflow.nodes.0.input.provider.apiKey",
      "workflow.nodes.0.input.auth.access_token",
      "workflow.nodes.0.input.credential_ref",
    ]);
  });

  it("rejects dependency cycles, missing dependencies, and forbidden operations before dispatch", () => {
    expect(workflowProblem([step("a"), step("b", ["a"])])).toBeNull();
    expect(workflowProblem([step("a", ["b"]), step("b", ["a"])])).toBe("cycle");
    expect(workflowProblem([step("a", ["missing"])])).toBe("dependency");
    expect(workflowProblem([{ ...step("write"), risk: "R3" }])).toBe(
      "forbidden",
    );
    expect(workflowProblem([{ ...step("a"), agent_id: "" }])).toBe("agent");
    expect(
      workflowProblem([{ ...step("echo"), skill: "local.echo", agent_id: "" }]),
    ).toBeNull();
  });

  it("prevents cookies and credential strings from being sent under ordinary input keys", () => {
    expect(
      credentialPaths({
        input: {
          cookie: "session=value",
          note: "Bearer sensitive-value",
          provider: "sk-12345678901234567890",
          max_tokens: 500,
        },
      }),
    ).toEqual(["input.cookie", "input.note", "input.provider"]);
  });
});
