import { credentialPaths } from "./localWorkspace";

export const EXAMPLE_EVALUATION_CASES = [
  {
    case_id: "echo-contract",
    title: "Echo contract",
    goal: "Return the expected message",
    input_overrides: { echo: { message: "expected" } },
    graders: [
      {
        id: "answer",
        type: "json_equals",
        path: "/nodes/echo/result/output/message",
        expected: "expected",
        required: true,
      },
      {
        id: "tool",
        type: "required_operation",
        operation: "local.echo",
        required: true,
      },
    ],
    tags: ["contract"],
    risk: "R0",
  },
];

export function parseEvaluationCases(text: string): Record<string, unknown>[] {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("cases");
  }
  const finite = (item: unknown): boolean => {
    if (typeof item === "number") return Number.isFinite(item);
    if (Array.isArray(item)) return item.every(finite);
    return item !== null && typeof item === "object"
      ? Object.values(item).every(finite)
      : true;
  };
  if (
    !Array.isArray(value) ||
    !value.length ||
    value.length > 64 ||
    !finite(value) ||
    value.some(
      (item) => !item || typeof item !== "object" || Array.isArray(item),
    )
  )
    throw new Error("cases");
  if (credentialPaths(value).length) throw new Error("credentials");
  return value as Record<string, unknown>[];
}

export function parseBenchmarkRunDirs(text: string): string[] {
  const paths = text
    .split(/\r?\n/)
    .map((path) => path.trim())
    .filter(Boolean);
  if (
    !paths.length ||
    paths.length > 64 ||
    paths.some(
      (path) =>
        /[\\:\0]/.test(path) ||
        path.startsWith("/") ||
        path.startsWith("~") ||
        path.split("/").some((part) => !part || part === "." || part === ".."),
    )
  )
    throw new Error("paths");
  return [...new Set(paths)];
}

export function evaluationMetric(value: unknown, percent = false): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return percent
    ? `${(value * 100).toFixed(1)}%`
    : Number.isInteger(value)
    ? String(value)
    : value.toFixed(3);
}
