import { describe, expect, it } from "vitest";
import type { LongtermTask } from "../../api/modules/longterm";
import {
  buildOperationBody,
  candidateActions,
  frozenBaseline,
  latestCandidates,
} from "./componentOperationsHelpers";
import en from "../../locales/componentOperations/en.json";
import zh from "../../locales/componentOperations/zh.json";

const task: LongtermTask = {
  task_id: "task-a",
  tenant_id: "tenant-a",
  space_id: "private-a",
  goal: "Verify sources",
  status: "completed",
  revision: 7,
  workflow: { nodes: [] },
};

describe("component operation contracts", () => {
  it("imports a relative run and preserves revision without accepting self-reported results", () => {
    expect(
      buildOperationBody("evaluate", task, {
        run_dir: "run-a/task-1",
        benchmark_task_id: "case-1",
      }),
    ).toEqual({
      expected_revision: 7,
      run_dir: "run-a/task-1",
      benchmark_task_id: "case-1",
    });
    for (const run_dir of [
      "../other",
      "a/../../b",
      "C:\\secrets",
      "/etc/results",
      "\\\\host\\share",
      "~/.private",
      "a//b",
    ]) {
      expect(() => buildOperationBody("evaluate", task, { run_dir })).toThrow(
        "relativeRunDir",
      );
    }
  });

  it("requires verified readback and consent for memory retention", () => {
    const values = {
      action: "retain",
      content: "Verified finding",
      consent: true,
      ttl_seconds: 3600,
    };
    expect(() => buildOperationBody("memory", task, values)).toThrow(
      "verifiedMemoryRequired",
    );
    const verified = {
      ...task,
      result: { goal_outcome: "achieved" },
      checkpoint: { verification: { evidence_refs: ["trusted:1"] } },
    };
    expect(buildOperationBody("memory", verified, values)).toEqual({
      action: "retain",
      content: "Verified finding",
      consent: true,
      ttl_seconds: 3600,
    });
    expect(() =>
      buildOperationBody("memory", verified, { ...values, consent: false }),
    ).toThrow("consentRequired");
  });

  it("validates recall budgets and deletion consent", () => {
    expect(() =>
      buildOperationBody("memory", task, {
        action: "recall",
        query: "source?",
        max_tokens: 0,
      }),
    ).toThrow("invalidNumber");
    expect(
      buildOperationBody("memory", task, {
        action: "recall",
        query: "source?",
      }),
    ).toEqual({ action: "recall", query: "source?", max_tokens: 2048 });
    expect(() =>
      buildOperationBody("memory", task, {
        action: "delete",
        document_id: "octop-doc",
      }),
    ).toThrow("consentRequired");
    expect(
      buildOperationBody("memory", task, {
        action: "delete",
        document_id: "octop-doc",
        consent: true,
      }),
    ).toEqual({ action: "delete", document_id: "octop-doc", consent: true });
  });

  it("requires explicit Semantica export consent", () => {
    expect(() => buildOperationBody("provenance", task, {})).toThrow(
      "consentRequired",
    );
    expect(buildOperationBody("provenance", task, { consent: true })).toEqual({
      consent: true,
    });
  });

  it("rejects authority or gate injection in nested proposal fields", () => {
    const values = {
      action: "propose",
      summary: "Check input",
      hypothesis: "Fewer omissions",
      benchmark_revision: "gold-v2",
    };
    for (const advanced_proposal of [
      '{"skills":[{"permissions":["write"]}]}',
      '{"hard_gates":{"authorization":true}}',
      '{"canaryReadiness":true}',
      '{"policy":{"allow":true}}',
    ]) {
      expect(() =>
        buildOperationBody("evolution", task, { ...values, advanced_proposal }),
      ).toThrow("proposalAuthority");
    }
    expect(() =>
      buildOperationBody("evolution", task, {
        ...values,
        advanced_proposal: "[]",
      }),
    ).toThrow("proposalObject");
    expect(buildOperationBody("evolution", task, values)).toEqual({
      action: "propose",
      expected_revision: 7,
      benchmark_revision: "gold-v2",
      proposal: { summary: "Check input", hypothesis: "Fewer omissions" },
    });
  });

  it("uses each candidate's latest persisted state and enforces transition confirmation", () => {
    const evolved = {
      ...task,
      artifacts: {
        evolution: [
          { id: "c1", status: "proposed" },
          { id: "c1", status: "canary" },
          { id: "c2", status: "rejected" },
        ],
      },
    };
    expect(
      latestCandidates(evolved).map((candidate) => candidate.status),
    ).toEqual(["rejected", "canary"]);
    expect(candidateActions("rejected")).toEqual([]);
    expect(() =>
      buildOperationBody("evolution", evolved, {
        action: "assess",
        candidate_id: "c1",
      }),
    ).toThrow("candidateActionInvalid");
    expect(() =>
      buildOperationBody("evolution", evolved, {
        action: "promoted",
        candidate_id: "c1",
      }),
    ).toThrow("confirmRequired");
    expect(
      buildOperationBody("evolution", evolved, {
        action: "promoted",
        candidate_id: "c1",
        confirm: true,
      }),
    ).toEqual({ action: "promoted", expected_revision: 7, candidate_id: "c1" });
  });

  it("builds RNA baseline only from frozen task skills and explicit strategy", () => {
    const frozen = {
      ...task,
      workflow: {
        nodes: [
          {
            id: "verify",
            agent_id: "a1",
            skill: "sources",
            risk: "R0" as const,
            input: {},
            depends_on: [],
            behavior_snapshot: {
              system_prompt: "Check sources first",
              skill_text: "Read trusted sources",
            },
          },
        ],
      },
    };
    expect(frozenBaseline(frozen).skills).toEqual([
      { id: "verify:sources", instructions: "Read trusted sources" },
    ]);
    const body = buildOperationBody("evolution", frozen, {
      action: "rna_propose",
      benchmark_revision: "gold-v1",
      strategy: "Verify facts",
      confirm: true,
    });
    expect(body).toEqual({
      action: "rna_propose",
      expected_revision: 7,
      benchmark_revision: "gold-v1",
      proposal: {
        baseline: {
          strategy: "Verify facts",
          skills: [
            { id: "verify:sources", instructions: "Read trusted sources" },
          ],
          assignments: {
            skillIds: ["verify:sources"],
            toolIds: [],
            mcpServerIds: [],
          },
        },
      },
    });
    expect(() =>
      buildOperationBody("evolution", frozen, {
        action: "rna_propose",
        benchmark_revision: "gold-v1",
        strategy: "Verify facts",
      }),
    ).toThrow("confirmRequired");
  });

  it("keeps component locale key trees identical", () => {
    const keys = (value: unknown, prefix = ""): string[] =>
      Object.entries(value as Record<string, unknown>).flatMap(
        ([key, child]) =>
          typeof child === "string"
            ? [`${prefix}${key}`]
            : keys(child, `${prefix}${key}.`),
      );
    expect(keys(en).sort()).toEqual(keys(zh).sort());
  });
});
