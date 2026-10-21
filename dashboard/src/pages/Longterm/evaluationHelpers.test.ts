import { describe, expect, it } from "vitest";
import {
  evaluationMetric,
  EXAMPLE_EVALUATION_CASES,
  parseBenchmarkRunDirs,
  parseEvaluationCases,
} from "./evaluationHelpers";

describe("evaluation editor boundaries", () => {
  it("accepts finite case documents and rejects credentials or missing cases", () => {
    expect(
      parseEvaluationCases(JSON.stringify(EXAMPLE_EVALUATION_CASES)),
    ).toEqual(EXAMPLE_EVALUATION_CASES);
    for (const text of [
      "{}",
      "[]",
      '[{"password":"hidden"}]',
      "[null]",
      '[{"score":1e999}]',
      "{bad-json",
    ])
      expect(() => parseEvaluationCases(text)).toThrow();
  });
  it("imports only tenant-relative report paths", () => {
    expect(parseBenchmarkRunDirs("run-a\r\nfolder/run-b\nrun-a")).toEqual([
      "run-a",
      "folder/run-b",
    ]);
    for (const path of [
      "../outside",
      "C:/private",
      "/etc/passwd",
      "~/.config",
      "a\\b",
      "a//b",
      "a/./b",
      "",
    ])
      expect(() => parseBenchmarkRunDirs(path)).toThrow();
  });
  it("keeps unknown metrics distinct from zero and never labels bounds as confidence intervals", () => {
    expect(evaluationMetric(undefined, true)).toBe("—");
    expect(evaluationMetric(null)).toBe("—");
    expect(evaluationMetric(0, true)).toBe("0.0%");
    expect(evaluationMetric(0.75, true)).toBe("75.0%");
  });
});
