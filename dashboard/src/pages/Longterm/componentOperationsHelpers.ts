import type { LongtermTask, TaskOperation } from "../../api/modules/longterm";

export type EvolutionAction =
  | "propose"
  | "rna_propose"
  | "assess"
  | "shadow"
  | "canary"
  | "promoted"
  | "rolled_back";

export interface OperationValues {
  action?: string;
  run_dir?: string;
  benchmark_task_id?: string;
  query?: string;
  content?: string;
  max_tokens?: number;
  ttl_seconds?: number;
  consent?: boolean;
  document_id?: string;
  benchmark_revision?: string;
  candidate_id?: string;
  summary?: string;
  hypothesis?: string;
  strategy?: string;
  advanced_proposal?: string;
  confirm?: boolean;
}

export class OperationValidationError extends Error {
  readonly key: string;
  constructor(key: string) {
    super(key);
    this.key = key;
  }
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function artifactRecords(task: LongtermTask, group: string) {
  const records = asRecord(task.artifacts)[group];
  return Array.isArray(records) ? records.map(asRecord) : [];
}

export function latestCandidates(task: LongtermTask) {
  const seen = new Set<string>();
  return artifactRecords(task, "evolution")
    .slice()
    .reverse()
    .filter((candidate) => {
      if (typeof candidate.id !== "string" || seen.has(candidate.id))
        return false;
      seen.add(candidate.id);
      return true;
    });
}

export function candidateActions(status: unknown): EvolutionAction[] {
  switch (status) {
    case "proposed":
      return ["assess"];
    case "evaluated":
      return ["shadow"];
    case "shadow":
      return ["canary"];
    case "canary":
      return ["promoted", "rolled_back"];
    case "promoted":
      return ["rolled_back"];
    default:
      return [];
  }
}

export function frozenBaseline(task: LongtermTask) {
  const skills = task.workflow.nodes.flatMap((node) => {
    const snapshot = asRecord(asRecord(node).behavior_snapshot);
    return node.skill && typeof snapshot.skill_text === "string"
      ? [{ id: `${node.id}:${node.skill}`, instructions: snapshot.skill_text }]
      : [];
  });
  const prompts = task.workflow.nodes
    .map((node) => asRecord(asRecord(node).behavior_snapshot).system_prompt)
    .filter(
      (prompt): prompt is string => typeof prompt === "string" && !!prompt,
    );
  return {
    strategy: [...new Set(prompts)].join("\n\n") || task.goal,
    skills,
    assignments: {
      skillIds: skills.map((skill) => skill.id),
      toolIds: [],
      mcpServerIds: [],
    },
  };
}

function required(value: unknown, key: string, maxLength: number): string {
  if (typeof value !== "string" || !value.trim() || value.length > maxLength) {
    throw new OperationValidationError(key);
  }
  return value.trim();
}

function bounded(value: unknown, min: number, max: number): number {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < min ||
    value > max
  ) {
    throw new OperationValidationError("invalidNumber");
  }
  return value;
}

const forbiddenProposalFields = new Set([
  "permissions",
  "grants",
  "tools",
  "toolids",
  "mcpserverids",
  "evaluationsuite",
  "policy",
  "assignments",
  "capabilities",
  "capabilityids",
  "riskfloor",
  "hardgates",
  "canaryreadiness",
  "actorisadmin",
  "evaluation",
]);

function checkProposal(value: unknown): void {
  if (Array.isArray(value)) {
    value.forEach(checkProposal);
  } else if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      if (forbiddenProposalFields.has(key.replace(/[_-]/g, "").toLowerCase())) {
        throw new OperationValidationError("proposalAuthority");
      }
      checkProposal(child);
    }
  }
}

export function buildOperationBody(
  kind: TaskOperation,
  task: LongtermTask,
  values: OperationValues,
): Record<string, unknown> {
  if (kind === "evaluate") {
    const runDir = required(values.run_dir, "runDirRequired", 400);
    if (
      /^(?:[\\/]|[A-Za-z]:|~)|:/.test(runDir) ||
      runDir.split(/[\\/]/).some((part) => part === ".." || part === "")
    ) {
      throw new OperationValidationError("relativeRunDir");
    }
    return {
      expected_revision: task.revision,
      run_dir: runDir,
      ...(values.benchmark_task_id?.trim()
        ? { benchmark_task_id: values.benchmark_task_id.trim() }
        : {}),
    };
  }
  if (kind === "provenance") {
    if (!values.consent) throw new OperationValidationError("consentRequired");
    return { consent: true };
  }
  if (kind === "memory") {
    if (!["recall", "retain", "delete"].includes(values.action ?? ""))
      throw new OperationValidationError("memoryActionInvalid");
    if (values.action === "recall") {
      return {
        action: "recall",
        query: required(values.query, "queryRequired", 8000),
        max_tokens: bounded(values.max_tokens ?? 2048, 128, 4096),
      };
    }
    if (!values.consent) throw new OperationValidationError("consentRequired");
    if (values.action === "delete") {
      return {
        action: "delete",
        document_id: required(values.document_id, "documentRequired", 200),
        consent: true,
      };
    }
    const refs = asRecord(task.checkpoint?.verification).evidence_refs;
    if (
      asRecord(task.result).goal_outcome !== "achieved" ||
      !Array.isArray(refs) ||
      !refs.length
    ) {
      throw new OperationValidationError("verifiedMemoryRequired");
    }
    return {
      action: "retain",
      content: required(values.content, "contentRequired", 16000),
      ttl_seconds: bounded(values.ttl_seconds ?? 2592000, 60, 31536000),
      consent: true,
    };
  }
  const action = values.action as EvolutionAction;
  const common = { action, expected_revision: task.revision };
  if (action === "propose" || action === "rna_propose") {
    const benchmark_revision = required(
      values.benchmark_revision,
      "benchmarkRequired",
      200,
    );
    if (action === "rna_propose") {
      if (!values.confirm)
        throw new OperationValidationError("confirmRequired");
      return {
        ...common,
        benchmark_revision,
        proposal: {
          baseline: {
            ...frozenBaseline(task),
            strategy: required(values.strategy, "strategyRequired", 32000),
          },
        },
      };
    }
    let extra: Record<string, unknown> = {};
    if (values.advanced_proposal?.trim()) {
      try {
        const parsed: unknown = JSON.parse(values.advanced_proposal);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
          throw new Error();
        extra = asRecord(parsed);
      } catch {
        throw new OperationValidationError("proposalObject");
      }
    }
    const proposal = {
      ...extra,
      summary: required(values.summary, "summaryRequired", 8000),
      hypothesis: required(values.hypothesis, "hypothesisRequired", 8000),
      ...(values.strategy?.trim() ? { strategy: values.strategy.trim() } : {}),
    };
    checkProposal(proposal);
    return { ...common, benchmark_revision, proposal };
  }
  const candidate = latestCandidates(task).find(
    (item) => item.id === values.candidate_id,
  );
  if (!candidate || !candidateActions(candidate.status).includes(action)) {
    throw new OperationValidationError("candidateActionInvalid");
  }
  if (
    ["canary", "promoted", "rolled_back"].includes(action) &&
    !values.confirm
  ) {
    throw new OperationValidationError("confirmRequired");
  }
  return { ...common, candidate_id: candidate.id };
}
