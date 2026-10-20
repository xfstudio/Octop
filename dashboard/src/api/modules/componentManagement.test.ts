import { beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../request";
import {
  componentManagementApi,
  ComponentLifecycleError,
} from "./componentManagement";

vi.mock("../request", () => ({ request: vi.fn() }));

describe("component lifecycle requests", () => {
  beforeEach(() => vi.clearAllMocks());

  it("sends one explicitly confirmed action with its observed revision", async () => {
    vi.mocked(request).mockResolvedValue({ component_id: "temporal" });
    const controller = new AbortController();
    await componentManagementApi.lifecycle(
      "temporal",
      "stop",
      12,
      controller.signal,
    );
    expect(request).toHaveBeenCalledExactlyOnceWith(
      "/longterm/integrations/temporal/lifecycle",
      {
        method: "POST",
        body: JSON.stringify({
          action: "stop",
          confirmed: true,
          expected_revision: 12,
        }),
        signal: controller.signal,
      },
    );
  });

  it("preserves a safe typed reason and status without retrying or exposing diagnostics", async () => {
    vi.mocked(request).mockRejectedValue(
      new Error(
        'Request failed: 409 Conflict - {"error":{"code":"RUNTIME_CONFLICT","message":"private deployment path","details":{"reason":"runtime_revision_conflict"}}}',
      ),
    );
    const cause = await componentManagementApi
      .lifecycle("rna", "start", 5)
      .catch((error: unknown) => error);
    expect(cause).toBeInstanceOf(ComponentLifecycleError);
    expect(cause).toMatchObject({
      status: 409,
      reason: "runtime_revision_conflict",
    });
    expect((cause as Error).message).not.toContain("private deployment path");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("uses a generic reason for unstructured errors", async () => {
    vi.mocked(request).mockRejectedValue(new Error("secret process stderr"));
    await expect(
      componentManagementApi.lifecycle("hindsight", "stop", 1),
    ).rejects.toMatchObject({
      status: undefined,
      reason: "lifecycle_failed",
      message: "Component lifecycle request failed",
    });
  });
});
