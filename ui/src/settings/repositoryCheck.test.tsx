import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { initI18n } from "../i18n/i18n";
import { fakeServer } from "../test/server";
import { useCheck } from "./repositoryCheck";

initI18n();

const QUEUED = {
  id: "p-1",
  kind: "access",
  state: "queued",
  outcome: null,
  message: null,
  host_keys: null,
  created_at: "2026-11-18T08:00:00Z",
  finished_at: null,
};

afterEach(() => {
  vi.useRealTimers();
});

describe("useCheck", () => {
  it("runs a check, waits, and gives up when left", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const server = fakeServer({
      "POST /api/v1/repository-probes": { status: 202, body: QUEUED },
      "GET /api/v1/repository-probes/p-1": { status: 200, body: QUEUED },
    });
    const { result, unmount } = renderHook(() => useCheck());

    act(() => {
      result.current.run({ kind: "access", repository: "analytics", environment: "dev" });
    });
    expect(result.current.busy).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.slow).toBe(true);
    await waitFor(() => {
      expect(server.sent.map((request) => request.method)).toEqual(["POST", "GET"]);
    });
    unmount();
    await vi.advanceTimersByTimeAsync(5000);

    expect(server.sent).toHaveLength(2);
  });
});
