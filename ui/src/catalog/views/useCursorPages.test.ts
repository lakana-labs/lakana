import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api";
import type { CursorPage } from "../../api/manage";
import { useCursorPages } from "./useCursorPages";

/**
 * A page of numbers.
 *
 * @param items - The numbers.
 * @param nextCursor - The cursor of the next page.
 * @returns The page.
 */
function page(items: number[], nextCursor: string | null): CursorPage<number> {
  return { items, nextCursor };
}

describe("useCursorPages", () => {
  it("reads the first page, appends the next with its cursor and stops at the last", async () => {
    const load = vi.fn((cursor: string | null) =>
      Promise.resolve(cursor === null ? page([1, 2], "c1") : page([3], null)),
    );
    const { result } = renderHook(() => useCursorPages("a", load));

    expect(result.current.items).toBeUndefined();
    expect(result.current.busy).toBe(true);
    await waitFor(() => {
      expect(result.current.items).toEqual([1, 2]);
    });
    expect(result.current.hasMore).toBe(true);

    await act(() => result.current.loadMore());

    expect(load).toHaveBeenLastCalledWith("c1");
    expect(result.current.items).toEqual([1, 2, 3]);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.busy).toBe(false);
  });

  it("starts again, without the old rows, when the key changes", async () => {
    const { result, rerender } = renderHook(
      ({ key }) => useCursorPages(key, (cursor) => Promise.resolve(page([key.length], cursor))),
      { initialProps: { key: "a" } },
    );
    await waitFor(() => {
      expect(result.current.items).toEqual([1]);
    });

    rerender({ key: "abc" });

    expect(result.current.items).toBeUndefined();
    await waitFor(() => {
      expect(result.current.items).toEqual([3]);
    });
  });

  it("says why the first page failed, and reads again on reload", async () => {
    let fail = true;
    const load = (): Promise<CursorPage<number>> =>
      fail
        ? Promise.reject(new ApiError(403, "ForbiddenError", "You may not read prod."))
        : Promise.resolve(page([7], null));
    const { result } = renderHook(() => useCursorPages("a", load));

    await waitFor(() => {
      expect(result.current.error).toBe("You may not read prod.");
    });
    expect(result.current.items).toBeUndefined();

    fail = false;
    act(() => {
      result.current.reload();
    });

    await waitFor(() => {
      expect(result.current.items).toEqual([7]);
    });
    expect(result.current.error).toBeNull();
  });

  it("keeps the rows and says why a later page failed", async () => {
    const load = (cursor: string | null): Promise<CursorPage<number>> =>
      cursor === null
        ? Promise.resolve(page([1], "c1"))
        : Promise.reject(new ApiError(500, "InternalError", "Try again in a moment."));
    const { result } = renderHook(() => useCursorPages("a", load));
    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });

    await act(() => result.current.loadMore());

    expect(result.current.items).toEqual([1]);
    expect(result.current.error).toBe("Try again in a moment.");
    expect(result.current.hasMore).toBe(true);
  });
});
