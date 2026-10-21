import { describe, expect, it } from "vitest";
import type { OperationSession } from "../../api/modules/operationSessions";
import {
  applySessionToDraft,
  MAX_DRAFT_INPUT_BYTES,
  resultFragment,
  sessionConsoleHref,
  sessionDraftInput,
  validateDraftResult,
} from "./sessionResults";

function savedSession(
  overrides: Partial<OperationSession> = {},
): OperationSession {
  const result = {
    source: "octop_node_result",
    component_id: "octop" as const,
    operation: "node_result",
    output: { query: "verified output", list: [1, 2] },
    verified: true,
    usage: { total_tokens: 0 },
    draft_only: true as const,
    promotion_allowed: false as const,
  };
  return {
    session_id: "session-1",
    component_id: "octop",
    operation: "node_result",
    input: {},
    task_id: "task-1",
    node_id: "seed",
    snapshot: {
      console: {
        url: "/workbench/platform?section=tasks",
        kind: "internal",
        available: true,
        label: "integration",
      },
    },
    status: "committed",
    revision: 3,
    candidate: result,
    result,
    result_digest: "a".repeat(64),
    source: "octop_node_result",
    created_at: 100,
    updated_at: 120,
    ...overrides,
  };
}

describe("component session result return", () => {
  it("selects only own JSON Pointer paths and wraps primitives as draft input", () => {
    expect(
      resultFragment({ "a/b": { "~key": ["selected"] } }, "/a~1b/~0key/0"),
    ).toBe("selected");
    expect(sessionDraftInput(savedSession(), "/output/query")).toEqual({
      value: "verified output",
    });
    for (const pointer of [
      "output",
      "/output/~3",
      "/output/missing",
      "/output/toString",
      "/output/list/01",
    ])
      expect(() => sessionDraftInput(savedSession(), pointer)).toThrow();
    expect(() =>
      sessionDraftInput(savedSession({ status: "ready", result: null })),
    ).toThrow();
  });

  it("blocks credential fields, credential strings, nonfinite JSON and oversized UTF-8 input", () => {
    for (const input of [
      { password: "hidden" },
      { note: "Bearer hidden" },
      { number: Infinity },
      { value: undefined },
      { value: "中".repeat(MAX_DRAFT_INPUT_BYTES / 3) },
    ])
      expect(() => validateDraftResult(input)).toThrow();
    expect(() =>
      validateDraftResult({ max_tokens: 4096, value: "ok" }),
    ).not.toThrow();
  });

  it("adds a local input node or replaces the selected draft without mutating frozen task data", () => {
    const nodes = [
      {
        id: "draft",
        skill: "local.echo",
        risk: "R0" as const,
        depends_on: [],
        input: { old: true },
      },
    ];
    const input = sessionDraftInput(savedSession());
    const appended = applySessionToDraft(nodes, input);
    expect(appended).toHaveLength(2);
    expect(appended[1]).toMatchObject({
      skill: "local.echo",
      risk: "R0",
      depends_on: [],
      input,
    });
    expect(applySessionToDraft(nodes, input, "draft")[0].input).toEqual(input);
    expect(nodes[0].input).toEqual({ old: true });
    expect(() => applySessionToDraft(nodes, input, "deleted")).toThrow();
  });

  it("opens only the validated server console and never URLs carrying credentials or arbitrary schemes", () => {
    expect(sessionConsoleHref(savedSession(), "http://localhost:8099")).toBe(
      "/workbench/platform?section=tasks",
    );
    for (const url of [
      "javascript:alert(1)",
      "http://user:password@localhost:3000",
      "https://example.com/?token=hidden",
      "https://example.com/#secret",
      "//example.com/console",
      "https://example.com/\\evil",
    ])
      expect(
        sessionConsoleHref(
          savedSession({
            snapshot: {
              console: {
                url,
                kind: "external",
                available: true,
                label: "native",
              },
            },
          }),
          "http://localhost:8099",
        ),
      ).toBeNull();
  });
});
