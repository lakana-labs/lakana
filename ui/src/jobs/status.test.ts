import { describe, expect, it } from "vitest";

import type { RunStatus, TaskStatus } from "../api/jobs";
import { failureCode, FAILURE_CODES, isActive, runStatusKind, taskStatusKind } from "./status";

describe("job statuses", () => {
  it("maps every run and task status to a glyph kind", () => {
    const runs: [RunStatus, string][] = [
      ["queued", "queued"],
      ["running", "running"],
      ["succeeded", "succeeded"],
      ["failed", "failed"],
      ["cancelled", "cancelled"],
    ];
    for (const [status, kind] of runs) {
      expect(runStatusKind(status)).toBe(kind);
    }
    const tasks: [TaskStatus, string][] = [
      ["pending", "queued"],
      ["ready", "queued"],
      ["running", "running"],
      ["succeeded", "succeeded"],
      ["failed", "failed"],
      ["upstream_failed", "upstream-failed"],
      ["cancelled", "cancelled"],
    ];
    for (const [status, kind] of tasks) {
      expect(taskStatusKind(status)).toBe(kind);
    }
  });

  it("knows which runs are not over", () => {
    expect(isActive({ status: "queued" })).toBe(true);
    expect(isActive({ status: "running" })).toBe(true);
    expect(isActive({ status: "failed" })).toBe(false);
    expect(isActive({ status: "succeeded" })).toBe(false);
  });

  it("shows unknown and newer failure codes as internal", () => {
    expect(failureCode("unknown")).toBe("internal");
    expect(failureCode("memory_spike")).toBe("internal");
    for (const code of FAILURE_CODES) {
      expect(failureCode(code)).toBe(code);
    }
    expect(FAILURE_CODES).toContain("worker_lost");
    expect(FAILURE_CODES).toHaveLength(18);
  });
});
