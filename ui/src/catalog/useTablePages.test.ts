import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { item } from "../test/catalog";
import { held } from "../test/pending";
import { fakeServer } from "../test/server";
import { useTablePages } from "./useTablePages";
import { DEFAULT_VIEW, type CatalogView } from "./view";

const FIRST = {
  items: [item({ name: "a" }), item({ name: "b" })],
  next_cursor: "c2",
  capped: false,
};
const SECOND = {
  items: [item({ name: "b" }), item({ name: "c" })],
  next_cursor: null,
  capped: false,
};

function names(items: readonly { name: string }[] | undefined): string[] {
  return (items ?? []).map((row) => row.name);
}

function render(view: CatalogView = DEFAULT_VIEW) {
  return renderHook(({ shown }) => useTablePages(shown), { initialProps: { shown: view } });
}

describe("useTablePages", () => {
  it("appends the next page and stops at the last", async () => {
    const server = fakeServer({
      "GET /api/v1/tables": (request) => ({
        status: 200,
        body: request.path.includes("cursor=c2") ? SECOND : FIRST,
      }),
    });
    const { result } = render();
    await waitFor(() => {
      expect(names(result.current.items)).toEqual(["a", "b"]);
    });
    expect(result.current.hasMore).toBe(true);
    expect(server.sent[0]?.path).not.toContain("cursor");

    await act(() => result.current.loadMore());

    expect(names(result.current.items)).toEqual(["a", "b", "c"]);
    expect(result.current.hasMore).toBe(false);
    expect(server.sent).toHaveLength(2);
  });

  it("starts again without a cursor when the sort changes", async () => {
    const server = fakeServer({ "GET /api/v1/tables": { status: 200, body: FIRST } });
    const { result, rerender } = render();
    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });
    await act(() => result.current.loadMore());
    expect(server.sent.at(-1)?.path).toContain("cursor=c2");

    rerender({ shown: { ...DEFAULT_VIEW, sort: "rows", order: "desc" } });

    await waitFor(() => {
      expect(server.sent.at(-1)?.path).toContain("sort=rows");
    });
    expect(server.sent.at(-1)?.path).not.toContain("cursor");
    await waitFor(() => {
      expect(result.current.busy).toBe(false);
    });
    expect(names(result.current.items)).toEqual(["a", "b"]);
  });

  it("drops a page that arrives after a newer view", async () => {
    const slow = held();
    fakeServer({
      "GET /api/v1/tables": (request) =>
        request.path.includes("q=sto&")
          ? slow.route()
          : {
              status: 200,
              body: { items: [item({ name: "store_id" })], next_cursor: null, capped: false },
            },
    });
    const { result, rerender } = render({ ...DEFAULT_VIEW, q: "sto" });
    rerender({ shown: { ...DEFAULT_VIEW, q: "store_id" } });
    await waitFor(() => {
      expect(names(result.current.items)).toEqual(["store_id"]);
    });

    await act(async () => {
      slow.release({
        status: 200,
        body: { items: [item({ name: "stolen" })], next_cursor: null, capped: false },
      });
      await Promise.resolve();
    });

    expect(names(result.current.items)).toEqual(["store_id"]);
    expect(result.current.busy).toBe(false);
  });

  it("drops a failure that arrives after a newer view", async () => {
    const slow = held();
    fakeServer({
      "GET /api/v1/tables": (request) =>
        request.path.includes("q=sto&") ? slow.route() : { status: 200, body: FIRST },
    });
    const { result, rerender } = render({ ...DEFAULT_VIEW, q: "sto" });
    rerender({ shown: { ...DEFAULT_VIEW, q: "store_id" } });
    await waitFor(() => {
      expect(names(result.current.items)).toEqual(["a", "b"]);
    });

    await act(async () => {
      slow.release({ status: 500, body: { detail: "The catalog is not reachable. Try again." } });
      await Promise.resolve();
    });

    expect(result.current.error).toBeNull();
    expect(names(result.current.items)).toEqual(["a", "b"]);
  });

  it.each([
    ["page", { status: 200, body: SECOND }],
    ["failure", { status: 500, body: { detail: "The catalog is not reachable. Try again." } }],
  ] as const)("drops a later %s that arrives after the view changed", async (_kind, answer) => {
    const slow = held();
    fakeServer({
      "GET /api/v1/tables": (request) =>
        request.path.includes("cursor=c2") ? slow.route() : { status: 200, body: FIRST },
    });
    const { result, rerender } = render();
    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });
    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = result.current.loadMore();
    });
    rerender({ shown: { ...DEFAULT_VIEW, sort: "rows", order: "desc" } });
    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });

    await act(async () => {
      slow.release(answer);
      await pending;
    });

    expect(names(result.current.items)).toEqual(["a", "b"]);
    expect(result.current.error).toBeNull();
    expect(result.current.hasMore).toBe(true);
  });

  it("keeps the previous rows while a new view loads", async () => {
    const slow = held();
    fakeServer({
      "GET /api/v1/tables": (request) =>
        request.path.includes("sort=rows") ? slow.route() : { status: 200, body: FIRST },
    });
    const { result, rerender } = render();
    await waitFor(() => {
      expect(names(result.current.items)).toEqual(["a", "b"]);
    });

    rerender({ shown: { ...DEFAULT_VIEW, sort: "rows", order: "desc" } });

    await waitFor(() => {
      expect(result.current.busy).toBe(true);
    });
    expect(names(result.current.items)).toEqual(["a", "b"]);
    expect(result.current.hasMore).toBe(false);

    await act(async () => {
      slow.release({ status: 200, body: SECOND });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(names(result.current.items)).toEqual(["b", "c"]);
    });
    expect(result.current.busy).toBe(false);
  });

  it("names the cause when the first page fails and reads again on reload", async () => {
    const server = fakeServer({
      "GET /api/v1/tables": {
        status: 500,
        body: { detail: "The catalog is not reachable. Try again." },
      },
    });
    const { result } = render();
    await waitFor(() => {
      expect(result.current.error).toBe("The catalog is not reachable. Try again.");
    });
    expect(result.current.items).toBeUndefined();

    server.route({ "GET /api/v1/tables": { status: 200, body: FIRST } });
    act(() => {
      result.current.reload();
    });

    await waitFor(() => {
      expect(names(result.current.items)).toEqual(["a", "b"]);
    });
    expect(result.current.error).toBeNull();
  });

  it("keeps the rows and names the cause when a later page fails", async () => {
    const server = fakeServer({ "GET /api/v1/tables": { status: 200, body: FIRST } });
    const { result } = render();
    await waitFor(() => {
      expect(result.current.hasMore).toBe(true);
    });
    server.route({
      "GET /api/v1/tables": {
        status: 500,
        body: { detail: "The catalog is not reachable. Try again." },
      },
    });

    await act(() => result.current.loadMore());

    expect(result.current.error).toBe("The catalog is not reachable. Try again.");
    expect(names(result.current.items)).toEqual(["a", "b"]);
    expect(result.current.hasMore).toBe(true);
  });

  it("does nothing when there is no next page", async () => {
    const server = fakeServer({
      "GET /api/v1/tables": { status: 200, body: { ...FIRST, next_cursor: null } },
    });
    const { result } = render();
    await waitFor(() => {
      expect(names(result.current.items)).toEqual(["a", "b"]);
    });

    await act(() => result.current.loadMore());

    expect(server.sent).toHaveLength(1);
  });

  it("reports that the server stopped looking", async () => {
    fakeServer({ "GET /api/v1/tables": { status: 200, body: { ...FIRST, capped: true } } });
    const { result } = render();

    await waitFor(() => {
      expect(result.current.capped).toBe(true);
    });
  });
});
