import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useNow } from "./useNow";

describe("useNow", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts at the present and ticks every 30 seconds, and stops on unmount", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-11-17T10:00:00Z"));
    const { result, unmount } = renderHook(() => useNow());
    expect(result.current).toBe(Date.parse("2026-11-17T10:00:00Z"));

    act(() => {
      vi.advanceTimersByTime(29_000);
    });
    expect(result.current).toBe(Date.parse("2026-11-17T10:00:00Z"));
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(result.current).toBe(Date.parse("2026-11-17T10:00:30Z"));

    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
