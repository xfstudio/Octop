import { describe, expect, it } from "vitest";
import {
  BeatDesignImportError,
  importBeatDesignCanvas,
} from "./beatdesignImport";
import { newLocalWorkspace, portableConfiguration } from "./localWorkspace";

const card = (id: string, binding: Record<string, unknown>) => ({
  id,
  prompt: `Research prompt\n\n\`\`\`octop-execution\n${JSON.stringify(
    binding,
  )}\n\`\`\``,
});

describe("BeatDesign Canvas executable bindings", () => {
  it("imports explicit prompt bindings and never infers dependencies from Canvas edges", () => {
    const result = importBeatDesignCanvas({
      document: {
        cards: [
          card("canvas-a", {
            id: "research",
            agent_id: "agent-a",
            skill: "agent.analyze",
            risk: "R0",
            depends_on: [],
          }),
          card("canvas-b", {
            id: "review",
            agent_id: "agent-b",
            skill: "review",
            risk: "R0",
            depends_on: [],
          }),
        ],
        edges: [{ fromCardId: "canvas-a", toCardId: "canvas-b" }],
      },
    });
    expect(result.nodes).toHaveLength(2);
    expect(result.nodes[1].depends_on).toEqual([]);
    expect(result.nodes[1].beatdesign_card_id).toBe("canvas-b");
    expect(result.nodes[0].input).toEqual({});
  });

  it("preserves sidecar execution limits and card identity through portable sync", () => {
    const workflow = importBeatDesignCanvas({
      document: {
        cards: [{ id: "c1", prompt: "Unchanged generation prompt" }],
      },
      octop_execution: {
        schema_version: 1,
        max_cycles: 3,
        max_parallel: 2,
        max_actions: 9,
        timeout_seconds: 500,
        nodes: [
          {
            card_id: "c1",
            id: "task",
            agent_id: "agent-a",
            skill: "analysis",
            risk: "R0",
            depends_on: [],
            resources: ["dataset"],
            timeout_seconds: 60,
          },
        ],
      },
    });
    const config = { ...newLocalWorkspace("Canvas").config, workflow };
    expect(portableConfiguration(config).workflow).toEqual(workflow);
  });

  it("rejects visual-only canvases, implicit dependencies, duplicate cards and execution cycles", () => {
    expect(() =>
      importBeatDesignCanvas({ cards: [{ id: "a", prompt: "Visual only" }] }),
    ).toThrow(BeatDesignImportError);
    expect(() =>
      importBeatDesignCanvas({
        cards: [card("a", { id: "work", skill: "analysis" })],
      }),
    ).toThrow(BeatDesignImportError);
    expect(() =>
      importBeatDesignCanvas({ cards: [{ id: "a" }, { id: "a" }] }),
    ).toThrow(BeatDesignImportError);
    expect(() =>
      importBeatDesignCanvas({
        cards: [
          card("a", { id: "one", skill: "analysis", depends_on: ["two"] }),
          card("b", { id: "two", skill: "analysis", depends_on: ["one"] }),
        ],
      }),
    ).toThrow("cycle");
  });
});
