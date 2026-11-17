import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import { POLL_BACKOFF_MAX_MS, POLL_FAILURES_SHOWN, useLivePoll } from "./useLivePoll";

let visibility: "visible" | "hidden" = "visible";

function show(state: "visible" | "hidden"): void {
  visibility = state;
  document.dispatchEvent(new Event("visibilitychange"));
}

async function tick(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useLivePoll", () => {
  it("reads now and every interval while visible", async () => {
    const load = vi.fn(() => Promise.resolve("a"));
    const { result } = renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(0);
    expect(load).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe("a");
    expect(result.current.stale).toBe(false);
    await tick(1000);
    await tick(1000);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("does not overlap a slow read with the next one", async () => {
    let finish: (value: string) => void = () => undefined;
    const load = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    );
    renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(5000);
    expect(load).toHaveBeenCalledTimes(1);
    finish("x");
    await tick(1000);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("skips while hidden and reads at once when shown", async () => {
    const load = vi.fn(() => Promise.resolve("a"));
    renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(0);
    act(() => {
      show("hidden");
    });
    await tick(10_000);
    expect(load).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    act(() => {
      show("visible");
    });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(2);
    await tick(1000);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("does not start a chain when a read ends in a hidden tab, nor when shown twice", async () => {
    let finish: (value: string) => void = () => undefined;
    const load = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    );
    renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    act(() => {
      show("hidden");
    });
    finish("a");
    await tick(5000);
    expect(load).toHaveBeenCalledTimes(1);
    act(() => {
      show("visible");
      show("visible");
    });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("starts nothing in a tab that is hidden from the first render", async () => {
    visibility = "hidden";
    const load = vi.fn(() => Promise.resolve("a"));
    renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(5000);
    expect(load).not.toHaveBeenCalled();
  });

  it("reads no more once active is false and again when it is true", async () => {
    const load = vi.fn(() => Promise.resolve("a"));
    const { rerender } = renderHook(({ active }) => useLivePoll(load, { everyMs: 1000, active }), {
      initialProps: { active: true },
    });
    await tick(0);
    rerender({ active: false });
    await tick(10_000);
    expect(load).toHaveBeenCalledTimes(1);
    act(() => {
      show("hidden");
      show("visible");
    });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(1);
    rerender({ active: true });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("doubles the wait after each failure up to a minute and keeps the last data as stale", async () => {
    let fail = false;
    const load = vi.fn(() =>
      fail ? Promise.reject(new ApiError(500, "HttpError", "Broken.")) : Promise.resolve("kept"),
    );
    const { result } = renderHook(() => useLivePoll(load, { everyMs: 10_000, active: true }));
    await tick(0);
    fail = true;
    await tick(10_000);
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.stale).toBe(true);
    expect(result.current.data).toBe("kept");
    expect(result.current.error).toBe("Broken.");
    await tick(19_999);
    expect(load).toHaveBeenCalledTimes(2);
    await tick(1);
    expect(load).toHaveBeenCalledTimes(3);
    await tick(40_000);
    expect(load).toHaveBeenCalledTimes(4);
    await tick(59_999);
    expect(load).toHaveBeenCalledTimes(4);
    await tick(1);
    expect(POLL_BACKOFF_MAX_MS).toBe(60_000);
    expect(load).toHaveBeenCalledTimes(5);
  });

  it("reports failures after three in a row and resets on success", async () => {
    let fail = true;
    const load = vi.fn(() =>
      fail ? Promise.reject(new ApiError(500, "HttpError", "Broken.")) : Promise.resolve("ok"),
    );
    const { result } = renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(0);
    expect(result.current.failures).toBe(1);
    await tick(2000);
    await tick(4000);
    expect(result.current.failures).toBe(POLL_FAILURES_SHOWN);
    expect(result.current.failure?.status).toBe(500);
    fail = false;
    await tick(8000);
    expect(result.current.failures).toBe(0);
    expect(result.current.error).toBeNull();
    expect(result.current.stale).toBe(false);
    expect(result.current.data).toBe("ok");
  });

  it("is not stale when nothing was ever read, and takes a non-API failure", async () => {
    const load = vi.fn(() => Promise.reject(new Error("boom")));
    const { result } = renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(0);
    expect(result.current.stale).toBe(false);
    expect(result.current.failure).toBeNull();
    expect(result.current.error).not.toBeNull();
  });

  it("reads at once on reload", async () => {
    const load = vi.fn(() => Promise.resolve("a"));
    const { result } = renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(0);
    act(() => {
      result.current.reload();
    });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("drops an answer that arrives after it was stopped", async () => {
    let finish: (value: string) => void = () => undefined;
    const load = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    );
    const { result, rerender } = renderHook(
      ({ active }) => useLivePoll(load, { everyMs: 1000, active }),
      { initialProps: { active: true } },
    );
    rerender({ active: false });
    finish("late");
    await tick(5000);
    expect(result.current.data).toBeUndefined();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("clears its timer and listener on unmount", async () => {
    const load = vi.fn(() => Promise.resolve("a"));
    const remove = vi.spyOn(document, "removeEventListener");
    const { unmount } = renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(0);
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    expect(remove).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
    act(() => {
      show("hidden");
      show("visible");
    });
    await tick(5000);
    expect(load).toHaveBeenCalledTimes(1);
  });
});

describe("useLivePoll without a visibility event", () => {
  it("skips a turn that falls in a hidden tab and reads on the next one once visible", async () => {
    vi.useFakeTimers();
    const load = vi.fn(() => Promise.resolve("a"));
    const state = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    await tick(0);
    state.mockReturnValue("hidden");
    await tick(1000);
    expect(load).toHaveBeenCalledTimes(1);
    state.mockReturnValue("visible");
    await tick(1000);
    expect(load).toHaveBeenCalledTimes(2);
  });
});

describe("useLivePoll with a read in flight", () => {
  it("drops a failure that arrives after it was stopped", async () => {
    vi.useFakeTimers();
    let fail: (cause: unknown) => void = () => undefined;
    const load = vi.fn(
      () =>
        new Promise<string>((_resolve, reject) => {
          fail = reject;
        }),
    );
    const { result, rerender } = renderHook(
      ({ active }) => useLivePoll(load, { everyMs: 1000, active }),
      { initialProps: { active: true } },
    );
    rerender({ active: false });
    fail(new ApiError(500, "HttpError", "Late."));
    await tick(5000);
    expect(result.current.failures).toBe(0);
  });

  it("does not start a second read when shown again while one is in flight", async () => {
    vi.useFakeTimers();
    const load = vi.fn(() => new Promise<string>(() => undefined));
    renderHook(() => useLivePoll(load, { everyMs: 1000, active: true }));
    act(() => {
      show("visible");
    });
    await tick(0);
    expect(load).toHaveBeenCalledTimes(1);
  });
});
