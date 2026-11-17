import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import type { QueryRunStatus } from "../api";
import { MeContext } from "../shell/me";
import { aPage, aRun, RUN_ID } from "../test/queries";
import { type FakeRoute, fakeServer } from "../test/server";
import {
  POLL_FAILURES,
  POLL_FAST_FOR_MS,
  POLL_FAST_MS,
  POLL_RETRY_MS,
  POLL_SLOW_MS,
  useQueryRun,
} from "./useQueryRun";

const RUNS = "/api/v1/environments/prod/queries";
const STATUS = `${RUNS}/${RUN_ID}`;
const FIRST_PAGE = `${STATUS}/result?format=json&limit=100&offset=0`;

/** Lets every answer the fake server gave reach the hook. */
async function settle(): Promise<void> {
  await act(async () => {
    for (let round = 0; round < 8; round += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
  });
}

/**
 * Moves the clock on, then lets the answers arrive.
 *
 * @param ms - The milliseconds.
 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
}

/**
 * Starts a fake server for one run in `prod`: the submit answers its id, the status answers the
 * states given, one per poll, and repeats the last, and the first page is one row.
 *
 * @param states - The states, in order.
 * @param more - Routes that differ.
 * @returns The server.
 */
function serve(
  states: readonly QueryRunStatus[],
  more: Record<string, FakeRoute> = {},
): ReturnType<typeof fakeServer> {
  let polls = 0;
  return fakeServer({
    [`POST ${RUNS}`]: { status: 202, body: { query_id: RUN_ID } },
    [`GET ${STATUS}`]: () => {
      const status = states[Math.min(polls, states.length - 1)] as QueryRunStatus;
      polls += 1;
      return { status: 200, body: aRun({ status }) };
    },
    [`GET ${STATUS}/result`]: { status: 200, body: aPage() },
    [`POST ${STATUS}/cancel`]: { status: 200, body: aRun({ status: "CANCELLED" }) },
    "POST /api/v1/principals/kim.lee/recent": { status: 204 },
    ...more,
  });
}

/**
 * The requests sent with a method to a path.
 *
 * @param server - The fake server.
 * @param method - The method.
 * @param path - The path, with its query.
 * @returns How many.
 */
function count(server: ReturnType<typeof fakeServer>, method: string, path: string): number {
  return server.sent.filter((request) => request.method === method && request.path === path).length;
}

/**
 * Places the hook inside a router at `/queries` and the signed-in person, `kim.lee`.
 *
 * @param props - The hook's tree.
 * @param props.children - The tree.
 * @returns The providers.
 */
function Wrapper({ children }: { readonly children: ReactNode }): ReactNode {
  return createElement(
    MeContext,
    {
      value: { name: "kim.lee", admin: false, mayDeploy: false, picture: null, noAccessYet: false },
    },
    createElement(MemoryRouter, { initialEntries: ["/queries"] }, children),
  );
}

/**
 * Renders the hook under a router at `/queries`, signed in as `kim.lee`.
 *
 * @returns The rendered hook: the run and the router's location.
 */
function render() {
  return renderHook(() => ({ query: useQueryRun(), location: useLocation() }), {
    wrapper: Wrapper,
  });
}

describe("useQueryRun", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("polls fast, then slow, as the plan says", () => {
    expect([POLL_FAST_MS, POLL_FAST_FOR_MS, POLL_SLOW_MS, POLL_RETRY_MS, POLL_FAILURES]).toEqual([
      500, 5_000, 2_000, 5_000, 3,
    ]);
  });

  it("submits, polls fast then slow, and reads the first page on success", async () => {
    const queued: QueryRunStatus[] = Array.from({ length: 12 }, () => "QUEUED");
    const server = serve([...queued, "RUNNING", "SUCCEEDED"]);
    const { result } = render();
    expect(result.current.query).toMatchObject({ phase: "idle", run: null, page: null });

    let submitted: Promise<void> = Promise.resolve();
    act(() => {
      submitted = result.current.query.submit("prod", "select 1 as one");
    });
    expect(result.current.query.phase).toBe("submitting");
    await act(() => submitted);
    await settle();

    expect(server.sent[0]).toMatchObject({
      method: "POST",
      path: RUNS,
      body: { sql: "select 1 as one" },
    });
    expect(result.current.location.pathname).toBe(`/queries/prod/runs/${RUN_ID}`);
    expect(result.current.location.state).toEqual({ keepFocus: true });
    expect(server.sent[1]).toMatchObject({
      method: "POST",
      path: "/api/v1/principals/kim.lee/recent",
      body: { kind: "query", environment: "prod", ref: RUN_ID },
    });
    const polls = (): number => count(server, "GET", STATUS);
    expect(polls()).toBe(1);
    expect(result.current.query).toMatchObject({ phase: "waiting", opened: false });
    expect(result.current.query.run?.status).toBe("QUEUED");

    await advance(POLL_FAST_MS - 1);
    expect(polls()).toBe(1);
    await advance(1);
    expect(polls()).toBe(2);
    await advance(4_500);
    expect(polls()).toBe(11);
    await advance(POLL_SLOW_MS - 1);
    expect(polls()).toBe(11);
    await advance(1);
    expect(polls()).toBe(12);
    await advance(POLL_SLOW_MS);
    expect(result.current.query.run?.status).toBe("RUNNING");
    await advance(POLL_SLOW_MS);

    expect(count(server, "GET", FIRST_PAGE)).toBe(1);
    expect(result.current.query).toMatchObject({ phase: "done", page: aPage(), error: null });
    const total = server.sent.length;
    await advance(10_000);
    expect(server.sent).toHaveLength(total);
  });

  it("a refused submit is refused with the ApiError", async () => {
    const server = serve(["SUCCEEDED"], {
      [`POST ${RUNS}`]: {
        status: 422,
        body: { detail: "Only one statement may run.", type: "QueryRefused" },
      },
    });
    const { result } = render();

    await act(() => result.current.query.submit("prod", "select 1; select 2"));
    await settle();

    expect(result.current.query.phase).toBe("refused");
    expect(result.current.query.error).toBeInstanceOf(ApiError);
    expect((result.current.query.error as ApiError).detail).toBe("Only one statement may run.");
    expect(result.current.location.pathname).toBe("/queries");
    expect(count(server, "GET", STATUS)).toBe(0);
  });

  it("three failed polls in a row stop with an error and retry starts again", async () => {
    let failing = true;
    const server = serve(["SUCCEEDED"], {
      [`GET ${STATUS}`]: () =>
        failing
          ? { status: 503, body: { detail: "The server is busy.", type: "Busy" } }
          : { status: 200, body: aRun() },
    });
    const { result } = render();
    await act(() => result.current.query.submit("prod", "select 1 as one"));
    await settle();
    const polls = (): number => count(server, "GET", STATUS);
    expect(polls()).toBe(1);
    expect(result.current.query.phase).toBe("waiting");

    await advance(POLL_RETRY_MS - 1);
    expect(polls()).toBe(1);
    await advance(1);
    expect(polls()).toBe(2);
    await advance(POLL_RETRY_MS);
    expect(polls()).toBe(3);

    expect(result.current.query.phase).toBe("failed");
    expect(result.current.query.failedRows).toBe(false);
    expect((result.current.query.error as ApiError).status).toBe(503);
    await advance(30_000);
    expect(polls()).toBe(3);

    failing = false;
    act(() => {
      result.current.query.retry();
    });
    expect(result.current.query.phase).toBe("waiting");
    await settle();

    expect(polls()).toBe(4);
    expect(result.current.query).toMatchObject({ phase: "done", page: aPage(), error: null });
    expect(count(server, "POST", "/api/v1/principals/kim.lee/recent")).toBe(1);
  });

  it("a poll that answers between failures resets their count", async () => {
    const answers = [503, 200, 503, 503, 200];
    let polls = 0;
    serve(["SUCCEEDED"], {
      [`GET ${STATUS}`]: () => {
        const status = answers[Math.min(polls, answers.length - 1)] as number;
        polls += 1;
        return status === 200
          ? { status, body: aRun({ status: polls < answers.length ? "QUEUED" : "SUCCEEDED" }) }
          : { status, body: { detail: "The server is busy.", type: "Busy" } };
      },
    });
    const { result } = render();
    await act(() => result.current.query.submit("prod", "select 1 as one"));
    await settle();

    // The second poll answers after 5 s of following, so the third waits the slow 2 s.
    await advance(POLL_RETRY_MS);
    await advance(POLL_SLOW_MS);
    await advance(POLL_RETRY_MS);
    // Two failures since the last answer, not three: still followed.
    expect(result.current.query.phase).toBe("waiting");
    await advance(POLL_RETRY_MS);

    expect(polls).toBe(5);
    expect(result.current.query.phase).toBe("done");
  });

  it("cancel posts once and reports true only while a run is active", async () => {
    let ended = false;
    const server = serve(["QUEUED"], {
      [`GET ${STATUS}`]: () => ({
        status: 200,
        body: aRun(
          ended
            ? { status: "CANCELLED", error_message: "Cancelled by kim.lee." }
            : { status: "QUEUED" },
        ),
      }),
    });
    const { result } = render();
    expect(result.current.query.cancel()).toBe(false);

    await act(() => result.current.query.submit("prod", "select 1 as one"));
    await settle();
    let first = false;
    let second = false;
    act(() => {
      first = result.current.query.cancel();
      second = result.current.query.cancel();
    });
    await settle();

    expect([first, second]).toEqual([true, true]);
    expect(count(server, "POST", `${STATUS}/cancel`)).toBe(1);
    expect(result.current.query).toMatchObject({ phase: "waiting", cancelling: true });

    ended = true;
    await advance(POLL_FAST_MS);

    expect(result.current.query).toMatchObject({ phase: "done", cancelling: false, page: null });
    expect(result.current.query.run?.status).toBe("CANCELLED");
    expect(result.current.query.cancel()).toBe(false);
    expect(count(server, "POST", `${STATUS}/cancel`)).toBe(1);
  });

  it("ignores a cancel the server refuses: the next poll says how the run ended", async () => {
    let ended = false;
    const server = serve(["QUEUED"], {
      [`GET ${STATUS}`]: () => ({
        status: 200,
        body: aRun({ status: ended ? "SUCCEEDED" : "RUNNING" }),
      }),
      [`POST ${STATUS}/cancel`]: {
        status: 409,
        body: { detail: "The query has ended.", type: "ConflictError" },
      },
    });
    const { result } = render();
    await act(() => result.current.query.submit("prod", "select 1 as one"));
    await settle();

    act(() => {
      result.current.query.cancel();
    });
    ended = true;
    await settle();
    await advance(POLL_FAST_MS);

    expect(count(server, "POST", `${STATUS}/cancel`)).toBe(1);
    expect(result.current.query).toMatchObject({
      phase: "done",
      page: aPage(),
      cancelling: false,
      cancelRefusal: null,
    });
  });

  it("a cancel refused for another reason says why and may be sent again", async () => {
    let busy = true;
    const server = serve(["RUNNING"], {
      [`POST ${STATUS}/cancel`]: () =>
        busy
          ? { status: 503, body: { detail: "The server is busy; try again.", type: "Busy" } }
          : { status: 200, body: aRun({ status: "CANCELLED" }) },
    });
    const { result } = render();
    await act(() => result.current.query.submit("prod", "select 1 as one"));
    await settle();

    act(() => {
      result.current.query.cancel();
    });
    await settle();

    expect(result.current.query).toMatchObject({ phase: "waiting", cancelling: false });
    expect((result.current.query.cancelRefusal as ApiError).status).toBe(503);

    busy = false;
    let again = false;
    act(() => {
      again = result.current.query.cancel();
    });
    expect(again).toBe(true);
    expect(result.current.query).toMatchObject({ cancelling: true, cancelRefusal: null });
    await settle();
    expect(count(server, "POST", `${STATUS}/cancel`)).toBe(2);
  });

  it("does not cancel on unmount", async () => {
    const server = serve(["QUEUED"]);
    const { result, unmount } = render();
    await act(() => result.current.query.submit("prod", "select 1 as one"));
    await settle();
    const sent = server.sent.length;

    window.dispatchEvent(new Event("pagehide"));
    unmount();
    await advance(60_000);

    expect(server.sent).toHaveLength(sent);
    expect(count(server, "POST", `${STATUS}/cancel`)).toBe(0);
  });

  it("opens a run from its address once, with its state and its first page", async () => {
    const server = serve(["SUCCEEDED"]);
    const { result } = render();

    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    expect(result.current.query.phase).toBe("waiting");
    await settle();
    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    await settle();

    expect(result.current.query).toMatchObject({ phase: "done", opened: true, page: aPage() });
    expect(result.current.query.run?.sql).toBe("select 1 as one");
    expect(count(server, "GET", STATUS)).toBe(1);
    expect(count(server, "POST", "/api/v1/principals/kim.lee/recent")).toBe(1);
  });

  it("goes on when the record for Home is refused", async () => {
    serve(["SUCCEEDED"], {
      "POST /api/v1/principals/kim.lee/recent": {
        status: 500,
        body: { detail: "Something broke.", type: "InternalError" },
      },
    });
    const { result } = render();

    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    await settle();

    expect(result.current.query).toMatchObject({ phase: "done", page: aPage(), error: null });
  });

  it("says a run has no stored result when its page answers 409", async () => {
    serve(["SUCCEEDED"], {
      [`GET ${STATUS}/result`]: {
        status: 409,
        body: { detail: "The query has no stored result.", type: "ConflictError" },
      },
    });
    const { result } = render();

    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    await settle();

    expect(result.current.query).toMatchObject({ phase: "done", page: null, error: null });
    expect(result.current.query.run?.status).toBe("SUCCEEDED");
  });

  it("a page read that fails otherwise fails, and retry reads it again", async () => {
    let failing = true;
    const server = serve(["SUCCEEDED"], {
      [`GET ${STATUS}/result`]: () =>
        failing
          ? { status: 500, body: { detail: "Something broke.", type: "InternalError" } }
          : { status: 200, body: aPage() },
    });
    const { result } = render();
    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    await settle();
    expect(result.current.query.phase).toBe("failed");
    expect(result.current.query.failedRows).toBe(true);
    expect(result.current.query.run?.status).toBe("SUCCEEDED");

    failing = false;
    act(() => {
      result.current.query.retry();
    });
    await settle();

    expect(result.current.query).toMatchObject({ phase: "done", page: aPage() });
    expect(count(server, "GET", FIRST_PAGE)).toBe(2);
  });

  it("retry does nothing before a run", () => {
    const server = serve(["SUCCEEDED"]);
    const { result } = render();

    act(() => {
      result.current.query.retry();
    });

    expect(result.current.query.phase).toBe("idle");
    expect(server.sent).toHaveLength(0);
  });

  it("more appends the next page and does nothing without one", async () => {
    const server = serve(["SUCCEEDED"], {
      [`GET ${STATUS}/result`]: (request) =>
        request.path.endsWith("offset=0")
          ? {
              status: 200,
              body: aPage({ rows: [[1], [2]], row_cut: [1], row_count: 3, next_offset: 2 }),
            }
          : {
              status: 200,
              body: aPage({ rows: [[3]], row_cut: [0], row_count: 3, columns_cut: 1 }),
            },
    });
    const { result } = render();
    await act(() => result.current.query.more());
    expect(server.sent).toHaveLength(0);

    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    await settle();
    await act(() => result.current.query.more());

    expect(count(server, "GET", `${STATUS}/result?format=json&limit=100&offset=2`)).toBe(1);
    expect(result.current.query.page).toMatchObject({
      rows: [[1], [2], [3]],
      row_cut: [1, 2],
      columns_cut: 1,
      next_offset: null,
    });
    const sent = server.sent.length;
    await act(() => result.current.query.more());
    expect(server.sent).toHaveLength(sent);
  });

  it("more rejects with the refusal and keeps the rows", async () => {
    serve(["SUCCEEDED"], {
      [`GET ${STATUS}/result`]: (request) =>
        request.path.endsWith("offset=0")
          ? { status: 200, body: aPage({ next_offset: 1, row_count: 2 }) }
          : { status: 500, body: { detail: "Something broke.", type: "InternalError" } },
    });
    const { result } = render();
    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    await settle();

    await act(() => expect(result.current.query.more()).rejects.toBeInstanceOf(ApiError));

    expect(result.current.query).toMatchObject({ phase: "done", page: { rows: [[1]] } });
  });

  it("drops what an earlier run answers once another started", async () => {
    let answer: () => void = () => undefined;
    const server = serve(["SUCCEEDED"], {
      [`GET ${STATUS}`]: () =>
        new Promise((resolve) => {
          answer = () => {
            resolve({ status: 200, body: aRun({ status: "QUEUED" }) });
          };
        }),
      "GET /api/v1/environments/dev/queries/other": {
        status: 200,
        body: aRun({ query_id: "other" }),
      },
      "GET /api/v1/environments/dev/queries/other/result": {
        status: 200,
        body: aPage({ rows: [[7]] }),
      },
      "POST /api/v1/environments/dev/queries": { status: 202, body: { query_id: "other" } },
    });
    const { result } = render();
    act(() => {
      result.current.query.open("prod", RUN_ID);
    });
    await settle();
    await act(() => result.current.query.submit("dev", "select 7"));
    await settle();

    answer();
    await settle();
    await advance(10_000);

    expect(result.current.query).toMatchObject({ phase: "done", page: { rows: [[7]] } });
    expect(count(server, "GET", STATUS)).toBe(1);
  });
});
