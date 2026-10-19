import type {
  WorkflowDefinition,
  WorkflowNode,
} from "../../api/modules/longterm";
import { workflowProblem } from "./localWorkspace";

type ImportErrorCode = "canvas" | "binding" | "empty" | "cycle" | "options";
export class BeatDesignImportError extends Error {
  readonly code: ImportErrorCode;
  constructor(code: ImportErrorCode) {
    super(code);
    this.code = code;
  }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Maps only explicit Octop bindings in real BeatDesign Canvas exports, using no network. */
export function importBeatDesignCanvas(envelope: unknown): WorkflowDefinition {
  if (!object(envelope)) throw new BeatDesignImportError("canvas");
  const canvas = envelope.document ?? envelope;
  if (!object(canvas) || !Array.isArray(canvas.cards))
    throw new BeatDesignImportError("canvas");
  const cards = new Map<string, Record<string, unknown>>();
  for (const card of canvas.cards) {
    if (!object(card) || typeof card.id !== "string" || cards.has(card.id))
      throw new BeatDesignImportError("canvas");
    cards.set(card.id, card);
  }
  let bindings: unknown[] = [];
  const options: Omit<WorkflowDefinition, "nodes"> = {};
  if (
    envelope.octop_execution !== undefined &&
    envelope.octop_execution !== null
  ) {
    const sidecar = envelope.octop_execution;
    if (
      !object(sidecar) ||
      sidecar.schema_version !== 1 ||
      !Array.isArray(sidecar.nodes)
    )
      throw new BeatDesignImportError("binding");
    bindings = sidecar.nodes;
    const optionBounds = {
      max_cycles: 32,
      max_actions: 4096,
      max_parallel: 16,
      timeout_seconds: 604800,
    };
    for (const [key, maximum] of Object.entries(optionBounds)) {
      const value = sidecar[key];
      if (value !== undefined) {
        if (
          typeof value !== "number" ||
          !Number.isInteger(value) ||
          value < 1 ||
          value > maximum
        )
          throw new BeatDesignImportError("options");
        options[key as keyof typeof optionBounds] = value;
      }
    }
  } else {
    for (const [cardId, card] of cards) {
      if (typeof card.prompt !== "string") continue;
      const blocks = [
        ...card.prompt.matchAll(/```octop-execution\s*\n([\s\S]*?)\n```/g),
      ];
      if (blocks.length > 1) throw new BeatDesignImportError("binding");
      if (blocks.length) {
        let binding: unknown;
        try {
          binding = JSON.parse(blocks[0][1]);
        } catch {
          throw new BeatDesignImportError("binding");
        }
        if (!object(binding)) throw new BeatDesignImportError("binding");
        bindings.push({ ...binding, card_id: cardId });
      }
    }
  }
  if (!bindings.length || bindings.length > 128)
    throw new BeatDesignImportError("empty");
  const usedCards = new Set<string>();
  const nodes: WorkflowNode[] = bindings.map((binding) => {
    if (
      !object(binding) ||
      typeof binding.card_id !== "string" ||
      !cards.has(binding.card_id) ||
      usedCards.has(binding.card_id) ||
      typeof binding.id !== "string" ||
      !binding.id.trim() ||
      binding.id.length > 160 ||
      typeof binding.skill !== "string" ||
      !binding.skill ||
      !Array.isArray(binding.depends_on) ||
      !binding.depends_on.every((id) => typeof id === "string") ||
      new Set(binding.depends_on).size !== binding.depends_on.length
    )
      throw new BeatDesignImportError("binding");
    usedCards.add(binding.card_id);
    const risk = binding.risk ?? "R2";
    if (
      !["R0", "R1", "R2", "R3"].includes(String(risk)) ||
      (binding.agent_id !== undefined &&
        typeof binding.agent_id !== "string") ||
      (binding.input !== undefined && !object(binding.input))
    )
      throw new BeatDesignImportError("binding");
    const resources = binding.resources ?? [];
    const timeout = binding.timeout_seconds ?? 300;
    if (
      !Array.isArray(resources) ||
      !resources.every((item) => typeof item === "string" && !!item) ||
      typeof timeout !== "number" ||
      !Number.isInteger(timeout) ||
      timeout < 1 ||
      timeout > 86400
    )
      throw new BeatDesignImportError("binding");
    return {
      id: binding.id,
      ...(binding.agent_id ? { agent_id: binding.agent_id as string } : {}),
      skill: binding.skill,
      depends_on: [...binding.depends_on].sort(),
      risk:
        binding.skill === "local.echo" ? "R0" : (risk as WorkflowNode["risk"]),
      input: object(binding.input) ? binding.input : {},
      resources: [...new Set(resources)].sort(),
      timeout_seconds: timeout,
      beatdesign_card_id: binding.card_id,
    };
  });
  const problem = workflowProblem(nodes);
  if (problem === "cycle") throw new BeatDesignImportError("cycle");
  if (problem && !["agent", "forbidden"].includes(problem))
    throw new BeatDesignImportError("binding");
  return { nodes, ...options };
}
