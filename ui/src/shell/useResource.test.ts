import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../api";
import { useResource } from "./useResource";

describe("useResource", () => {
  it("drops an answer that arrives after a newer read started", async () => {
    let first: (value: string) => void = () => undefined;
    const slow = (): Promise<string> =>
      new Promise((resolve) => {
        first = resolve;
      });
    const fast = (): Promise<string> => Promise.resolve("new");
    const { result, rerender } = renderHook(({ load }) => useResource(load), {
      initialProps: { load: slow },
    });

    rerender({ load: fast });
    await waitFor(() => {
      expect(result.current.data).toBe("new");
    });
    act(() => {
      first("old");
    });

    await Promise.resolve();
    expect(result.current.data).toBe("new");
  });

  it("drops a late failure too, and keeps the last answer on a new failure", async () => {
    let fail: (error: unknown) => void = () => undefined;
    const failing = (): Promise<string> =>
      new Promise((_resolve, reject) => {
        fail = reject;
      });
    const { result, rerender } = renderHook(({ load }) => useResource(load), {
      initialProps: { load: failing },
    });
    const good = (): Promise<string> => Promise.resolve("kept");
    rerender({ load: good });
    await waitFor(() => {
      expect(result.current.data).toBe("kept");
    });
    act(() => {
      fail(new ApiError(500, "HttpError", "late"));
    });
    await Promise.resolve();
    expect(result.current.error).toBeNull();

    const bad = (): Promise<string> => Promise.reject(new ApiError(500, "HttpError", "Broken."));
    rerender({ load: bad });

    await waitFor(() => {
      expect(result.current.error).toBe("Broken.");
    });
    expect(result.current.data).toBe("kept");
  });
});
