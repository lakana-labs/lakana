import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { useAfter, WORDS_AFTER_MS } from "./useAfter";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});
afterEach(() => {
  vi.useRealTimers();
});

it("turns true a second after the component appeared", () => {
  const { result } = renderHook(() => useAfter());
  expect(WORDS_AFTER_MS).toBe(1_000);

  act(() => {
    vi.advanceTimersByTime(999);
  });
  expect(result.current).toBe(false);
  act(() => {
    vi.advanceTimersByTime(1);
  });

  expect(result.current).toBe(true);
});

it("stops its timer when the component goes", () => {
  const { unmount } = renderHook(() => useAfter(50));

  unmount();

  expect(vi.getTimerCount()).toBe(0);
});
