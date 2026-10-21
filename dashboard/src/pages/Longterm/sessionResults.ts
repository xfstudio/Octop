import type { WorkflowNode } from "../../api/modules/longterm";
import type { OperationSession } from "../../api/modules/operationSessions";
import { credentialPaths, localIdentifier } from "./localWorkspace";

export class SessionResultError extends Error {
  readonly key: "json" | "credentials" | "size" | "pointer" | "node";
  constructor(key: "json" | "credentials" | "size" | "pointer" | "node") {
    super(key);
    this.key = key;
  }
}

export const MAX_DRAFT_INPUT_BYTES = 128 * 1024;

/** Result data remains ordinary draft input, never a command or approval. */
export function validateDraftResult(value: unknown): void {
  const finiteJson = (item: unknown): boolean => {
    if (item === null || typeof item === "string" || typeof item === "boolean")
      return true;
    if (typeof item === "number") return Number.isFinite(item);
    if (Array.isArray(item)) return item.every(finiteJson);
    return (
      !!item &&
      typeof item === "object" &&
      Object.values(item).every(finiteJson)
    );
  };
  if (!finiteJson(value)) throw new SessionResultError("json");
  if (credentialPaths(value).length)
    throw new SessionResultError("credentials");
  if (
    new TextEncoder().encode(JSON.stringify(value)).length >
    MAX_DRAFT_INPUT_BYTES
  )
    throw new SessionResultError("size");
}

export function resultFragment(value: unknown, pointer: string): unknown {
  if (pointer === "") return value;
  if (!pointer.startsWith("/") || /~(?:[^01]|$)/.test(pointer))
    throw new SessionResultError("pointer");
  let current = value;
  for (const encoded of pointer.slice(1).split("/")) {
    const key = encoded.replace(/~1/g, "/").replace(/~0/g, "~");
    if (
      current === null ||
      typeof current !== "object" ||
      (Array.isArray(current) && !/^(?:0|[1-9][0-9]*)$/.test(key)) ||
      !Object.prototype.hasOwnProperty.call(current, key)
    )
      throw new SessionResultError("pointer");
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

export function sessionDraftInput(
  session: OperationSession,
  pointer = "/output",
): Record<string, unknown> {
  if (session.status !== "committed" || !session.result)
    throw new SessionResultError("json");
  const selected = resultFragment(session.result, pointer);
  const input =
    selected !== null &&
    typeof selected === "object" &&
    !Array.isArray(selected)
      ? (selected as Record<string, unknown>)
      : { value: selected };
  validateDraftResult(input);
  return JSON.parse(JSON.stringify(input)) as Record<string, unknown>;
}

export function applySessionToDraft(
  nodes: WorkflowNode[],
  input: Record<string, unknown>,
  targetId?: string,
): WorkflowNode[] {
  validateDraftResult(input);
  if (targetId) {
    if (!nodes.some((node) => node.id === targetId))
      throw new SessionResultError("node");
    return nodes.map((node) =>
      node.id === targetId ? { ...node, input } : node,
    );
  }
  return [
    ...nodes,
    {
      id: `component_result_${localIdentifier()
        .replace(/-/g, "")
        .slice(0, 12)}`,
      skill: "local.echo",
      risk: "R0",
      depends_on: [],
      input,
    },
  ];
}

/** Only a server-selected console can be opened; URL payloads never carry credentials. */
export function sessionConsoleHref(session: OperationSession, origin: string) {
  const console = session.snapshot.console;
  if (!console?.available || !console.url) return null;
  try {
    const url = new URL(console.url, origin);
    if (url.username || url.password || console.url.includes("\\")) return null;
    if (console.kind === "internal")
      return console.url.startsWith("/") &&
        !console.url.startsWith("//") &&
        url.origin === origin
        ? `${url.pathname}${url.search}${url.hash}`
        : null;
    return /^https?:\/\//i.test(console.url) && !url.search && !url.hash
      ? url.href
      : null;
  } catch {
    return null;
  }
}
