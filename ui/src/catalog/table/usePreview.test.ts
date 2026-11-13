import { act, renderHook } from "@testing-library/react";
import i18next from "i18next";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { vi } from "vitest";

import type { QueryRunStatus } from "../../api";
import { MeContext } from "../../shell/me";
import { fakeServer, type FakeRoute } from "../../test/server";
import {
  permissions,
  QUERIES_PATH,
  QUERY_ID,
  queryStatus,
  RESULT,
  TABLE_PATH,
} from "../../test/table";
import { usePreview } from "./usePreview";

const REF = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const STATUS = `GET ${QUERIES_PATH}/${QUERY_ID}`;

/**
 * Provides the signed-in person, `admin`.
 *
 * @param props - The hook's tree.
 * @param props.children - The tree.
 * @returns The provider.
 */
function Me({ children }: { readonly children: ReactNode }): ReactNode {
  return createElement(
    MeContext,
    {
      value: { name: "admin", admin: true, mayDeploy: true, picture: null, noAccessYet: false },
    },
    children,
  );
}

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
 * Starts a fake server that answers the preview's routes; the status route answers the states
 * given, one per poll, and repeats the last.
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
    "GET /api/v1/principals/admin/permissions": {
      status: 200,
      body: permissions("admin", ["SUBMIT_QUERY"]),
    },
    [`POST ${TABLE_PATH}/preview`]: { status: 202, body: { query_id: QUERY_ID } },
    [STATUS]: () => {
      const state = states[Math.min(polls, states.length - 1)] as QueryRunStatus;
      polls += 1;
      return { status: 200, body: queryStatus(state) };
    },
    [`GET ${QUERIES_PATH}/${QUERY_ID}/result`]: { status: 200, body: RESULT },
    [`POST ${QUERIES_PATH}/${QUERY_ID}/cancel`]: { status: 200, body: queryStatus("CANCELLED") },
    ...more,
  });
}

/**
 * The requests sent with a method to a path that starts with a prefix.
 *
 * @param server - The fake server.
 * @param method - The method.
 * @param prefix - The start of the path.
 * @returns The paths.
 */
function sentTo(server: ReturnType<typeof fakeServer>, method: string, prefix: string): string[] {
  return server.sent
    .filter((request) => request.method === method && request.path.startsWith(prefix))
    .map((request) => request.path);
}

/**
 * Renders the hook for the fact table.
 *
 * @param kind - What the table is.
 * @returns The rendered hook.
 */
function preview(kind: "TABLE" | "VIEW" | "ARTIFACT" = "TABLE") {
  return renderHook(() => usePreview(REF, kind), { wrapper: Me });
}

describe("usePreview", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("asks for the right to run queries in the environment, then queues the preview", async () => {
    const server = serve(["SUCCEEDED"]);
    const { result } = preview();
    expect(result.current.phase).toBe("checking");

    await settle();

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/principals/admin/permissions?environment=prod",
      `POST ${TABLE_PATH}/preview`,
      STATUS,
      `GET ${QUERIES_PATH}/${QUERY_ID}/result?format=json&limit=100`,
    ]);
    expect(result.current).toMatchObject({ phase: "done", status: "SUCCEEDED", page: RESULT });
  });

  it("polls every half second, then every two", async () => {
    const states: QueryRunStatus[] = Array.from({ length: 12 }, () => "QUEUED");
    const server = serve([...states, "RUNNING", "SUCCEEDED"]);
    const { result } = preview();
    await settle();
    const polls = (): number => sentTo(server, "GET", `${QUERIES_PATH}/${QUERY_ID}`).length;
    expect(polls()).toBe(1);
    expect(result.current).toMatchObject({ phase: "waiting", status: "QUEUED" });

    await advance(499);
    expect(polls()).toBe(1);
    await advance(1);
    expect(polls()).toBe(2);
    await advance(4_500);
    expect(polls()).toBe(11);
    await advance(1_999);
    expect(polls()).toBe(11);
    await advance(1);
    expect(polls()).toBe(12);
    await advance(2_000);
    expect(result.current).toMatchObject({ phase: "waiting", status: "RUNNING" });
    await advance(2_000);
    expect(result.current.phase).toBe("done");

    const total = server.sent.length;
    await advance(10_000);
    expect(server.sent).toHaveLength(total);
  });

  it("cancels the run when the tab is left", async () => {
    const server = serve(["QUEUED"]);
    const { unmount } = preview();
    await settle();

    unmount();
    await settle();

    expect(sentTo(server, "POST", QUERIES_PATH)).toEqual([`${QUERIES_PATH}/${QUERY_ID}/cancel`]);
  });

  it("cancels a run whose submission answers after the tab was left", async () => {
    let answer: (id: string) => void = () => undefined;
    const server = serve(["QUEUED"], {
      [`POST ${TABLE_PATH}/preview`]: () =>
        new Promise((resolve) => {
          answer = (id) => {
            resolve({ status: 202, body: { query_id: id } });
          };
        }),
    });
    const { unmount } = preview();
    await settle();
    unmount();

    answer(QUERY_ID);
    await settle();

    expect(sentTo(server, "POST", QUERIES_PATH)).toEqual([`${QUERIES_PATH}/${QUERY_ID}/cancel`]);
    expect(sentTo(server, "GET", QUERIES_PATH)).toEqual([]);
  });

  it("leaves no orphan run under StrictMode", async () => {
    let next = 0;
    const server = serve(["QUEUED"], {
      [`POST ${TABLE_PATH}/preview`]: () => {
        next += 1;
        return { status: 202, body: { query_id: `run-${String(next)}` } };
      },
    });
    for (const id of ["run-1", "run-2"]) {
      server.route({
        [`GET ${QUERIES_PATH}/${id}`]: { status: 200, body: queryStatus("QUEUED") },
        [`POST ${QUERIES_PATH}/${id}/cancel`]: { status: 200, body: queryStatus("CANCELLED") },
      });
    }
    const { unmount } = renderHook(() => usePreview(REF, "TABLE"), {
      wrapper: Me,
      reactStrictMode: true,
    });
    await settle();

    const submitted = Array.from({ length: next }, (_, index) => `run-${String(index + 1)}`);
    const cancelled = sentTo(server, "POST", QUERIES_PATH).map((path) => path.split("/")[6]);
    const live = submitted.filter((id) => !cancelled.includes(id));
    expect(live).toHaveLength(1);
    expect(sentTo(server, "GET", QUERIES_PATH)).toEqual([`${QUERIES_PATH}/${String(live[0])}`]);

    unmount();
    await settle();
    const after = sentTo(server, "POST", QUERIES_PATH).map((path) => path.split("/")[6]);
    expect(submitted.filter((id) => !after.includes(id))).toEqual([]);
  });

  it("ignores a cancel the server refuses", async () => {
    const server = serve(["QUEUED"], {
      [`POST ${QUERIES_PATH}/${QUERY_ID}/cancel`]: {
        status: 409,
        body: { detail: "The query has ended.", type: "ConflictError" },
      },
    });
    const { unmount } = preview();
    await settle();

    unmount();
    await settle();

    expect(sentTo(server, "POST", QUERIES_PATH)).toEqual([`${QUERIES_PATH}/${QUERY_ID}/cancel`]);
  });

  it("drops a poll or a page that answers after the tab was left", async () => {
    let answer: () => void = () => undefined;
    const late =
      (reply: { readonly status: number; readonly body: unknown }): FakeRoute =>
      () =>
        new Promise((resolve) => {
          answer = () => {
            resolve(reply);
          };
        });
    const polled = serve(["QUEUED"], {
      [STATUS]: late({ status: 200, body: queryStatus("QUEUED") }),
    });
    const first = preview();
    await settle();
    first.unmount();
    answer();
    await advance(10_000);
    expect(sentTo(polled, "GET", QUERIES_PATH)).toEqual([STATUS.slice(4)]);

    for (const page of [
      { status: 200, body: RESULT },
      { status: 503, body: { detail: "Busy.", type: "ServiceUnavailable" } },
    ]) {
      const read = serve(["SUCCEEDED"], { [`GET ${QUERIES_PATH}/${QUERY_ID}/result`]: late(page) });
      const second = preview();
      await settle();
      const shown = second.result.current;
      second.unmount();
      answer();
      await advance(10_000);
      expect(second.result.current).toBe(shown);
      expect(sentTo(read, "GET", `${QUERIES_PATH}/${QUERY_ID}/result`)).toHaveLength(1);
    }
  });

  it("cancels a waiting run when the page goes away, with a request that outlives the page", async () => {
    const server = serve(["QUEUED"]);
    const { unmount } = preview();
    await settle();

    window.dispatchEvent(new Event("pagehide"));
    await settle();
    unmount();
    window.dispatchEvent(new Event("pagehide"));
    await settle();

    const cancels = server.sent.filter((request) => request.path.endsWith("/cancel"));
    expect(
      cancels.map((request) => ({
        path: request.path,
        keepalive: request.keepalive,
        background: request.headers["x-lakana-background"],
      })),
    ).toEqual([{ path: `${QUERIES_PATH}/${QUERY_ID}/cancel`, keepalive: true, background: "1" }]);
  });

  it("does not cancel a finished run when the page goes away", async () => {
    const server = serve(["SUCCEEDED"]);
    preview();
    await settle();

    window.dispatchEvent(new Event("pagehide"));
    await settle();

    expect(sentTo(server, "POST", QUERIES_PATH)).toEqual([]);
  });

  it("keeps the run when the language changes", async () => {
    const server = serve(["QUEUED"]);
    preview();
    await settle();

    try {
      await act(async () => {
        await i18next.changeLanguage("en-GB");
      });
      await settle();
    } finally {
      await act(async () => {
        await i18next.changeLanguage("en");
      });
    }

    expect(sentTo(server, "POST", "/")).toEqual([`${TABLE_PATH}/preview`]);
  });

  it("stops polling after the tab is left", async () => {
    const server = serve(["QUEUED"]);
    const { unmount } = preview();
    await settle();
    unmount();
    await settle();
    const polls = sentTo(server, "GET", QUERIES_PATH).length;

    await advance(10_000);

    expect(sentTo(server, "GET", QUERIES_PATH)).toHaveLength(polls);
  });

  it.each<QueryRunStatus>(["SUCCEEDED", "FAILED", "CANCELLED"])(
    "does not cancel a finished run (%s)",
    async (state) => {
      const server = serve([state]);
      const { result, unmount } = preview();
      await settle();
      expect(result.current.status).toBe(state);

      unmount();
      await settle();

      expect(sentTo(server, "POST", QUERIES_PATH)).toEqual([]);
    },
  );

  it("retries a busy result page, then says so", async () => {
    const server = serve(["SUCCEEDED"], {
      [`GET ${QUERIES_PATH}/${QUERY_ID}/result`]: {
        status: 503,
        body: { detail: "Busy.", type: "ServiceUnavailable" },
      },
    });
    const { result } = preview();
    await settle();
    const reads = (): number => sentTo(server, "GET", `${QUERIES_PATH}/${QUERY_ID}/result`).length;
    expect(reads()).toBe(1);

    for (let retry = 1; retry <= 5; retry += 1) {
      expect(result.current.phase).toBe("waiting");
      await advance(1_000);
      expect(reads()).toBe(1 + retry);
    }

    expect(result.current).toMatchObject({
      phase: "failed",
      error: "The server is busy preparing results. Try again.",
    });
    await advance(10_000);
    expect(reads()).toBe(6);
  });

  it("reads the page once the server is no longer busy", async () => {
    let reads = 0;
    serve(["SUCCEEDED"], {
      [`GET ${QUERIES_PATH}/${QUERY_ID}/result`]: () => {
        reads += 1;
        return reads < 3
          ? { status: 503, body: { detail: "Busy.", type: "ServiceUnavailable" } }
          : { status: 200, body: RESULT };
      },
    });
    const { result } = preview();
    await settle();

    await advance(2_000);

    expect(result.current).toMatchObject({ phase: "done", page: RESULT });
  });

  it("says why a run did not finish, in the server's words or its own", async () => {
    serve(["FAILED"], {
      [STATUS]: {
        status: 200,
        body: queryStatus("FAILED", {
          error_message: "The table changed since the preview was asked for; refresh.",
        }),
      },
    });
    const { result } = preview();
    await settle();
    expect(result.current).toMatchObject({
      phase: "failed",
      error: "The table changed since the preview was asked for; refresh.",
    });

    serve(["CANCELLED"]);
    const other = preview();
    await settle();
    expect(other.result.current).toMatchObject({
      phase: "failed",
      status: "CANCELLED",
      error: "The preview did not finish. Refresh to run it again.",
    });
  });

  it("shows the server's sentence when the preview or a poll is refused", async () => {
    serve(["QUEUED"], {
      [`POST ${TABLE_PATH}/preview`]: {
        status: 409,
        body: { detail: "The table changed.", type: "ConflictError" },
      },
    });
    const { result } = preview();
    await settle();
    expect(result.current).toMatchObject({ phase: "failed", error: "The table changed." });

    serve(["QUEUED"], {
      [STATUS]: { status: 404, body: { detail: "No such query.", type: "NotFoundError" } },
    });
    const polled = preview();
    await settle();
    expect(polled.result.current).toMatchObject({ phase: "failed", error: "No such query." });

    serve(["SUCCEEDED"], {
      [`GET ${QUERIES_PATH}/${QUERY_ID}/result`]: {
        status: 403,
        body: { detail: "Not yours.", type: "PermissionDenied" },
      },
    });
    const read = preview();
    await settle();
    expect(read.result.current).toMatchObject({ phase: "failed", error: "Not yours." });
  });

  it("sends nothing for a person without the right to run queries", async () => {
    const server = serve(["SUCCEEDED"], {
      "GET /api/v1/principals/admin/permissions": {
        status: 200,
        body: permissions("admin", ["READ_TABLE"]),
      },
    });
    const { result } = preview();
    await settle();

    expect(result.current.phase).toBe("not_allowed");
    expect(sentTo(server, "POST", "/")).toEqual([]);
  });

  it("asks nothing for an artifact", async () => {
    const server = serve(["SUCCEEDED"]);
    const { result } = preview("ARTIFACT");
    await settle();

    expect(result.current.phase).toBe("artifact");
    expect(server.sent).toEqual([]);
  });

  it("cancels a waiting run and runs again on refresh", async () => {
    let next = 0;
    const server = serve(["QUEUED"], {
      [`POST ${TABLE_PATH}/preview`]: () => {
        next += 1;
        return { status: 202, body: { query_id: next === 1 ? QUERY_ID : "run-2" } };
      },
      [`GET ${QUERIES_PATH}/run-2`]: { status: 200, body: queryStatus("SUCCEEDED") },
      [`GET ${QUERIES_PATH}/run-2/result`]: { status: 200, body: RESULT },
    });
    const { result } = preview();
    await settle();

    act(() => {
      result.current.refresh();
    });
    expect(result.current.phase).toBe("checking");
    await settle();

    expect(sentTo(server, "POST", QUERIES_PATH)).toEqual([`${QUERIES_PATH}/${QUERY_ID}/cancel`]);
    expect(result.current).toMatchObject({ phase: "done", page: RESULT });
  });

  it("runs a fresh preview when the page returns from the back-forward cache", async () => {
    let next = 0;
    const server = serve(["QUEUED"], {
      [`POST ${TABLE_PATH}/preview`]: () => {
        next += 1;
        return { status: 202, body: { query_id: next === 1 ? QUERY_ID : "run-2" } };
      },
      [`GET ${QUERIES_PATH}/run-2`]: { status: 200, body: queryStatus("SUCCEEDED") },
      [`GET ${QUERIES_PATH}/run-2/result`]: { status: 200, body: RESULT },
    });
    const { result } = preview();
    await settle();
    window.dispatchEvent(new Event("pagehide"));
    await settle();

    act(() => {
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    });
    await settle();

    expect(sentTo(server, "POST", `${TABLE_PATH}/preview`)).toHaveLength(2);
    expect(result.current).toMatchObject({ phase: "done", page: RESULT });
  });

  it("does not run again for a pageshow that is no return from the cache", async () => {
    const server = serve(["QUEUED"]);
    preview();
    await settle();

    act(() => {
      window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: false }));
    });
    await settle();

    expect(sentTo(server, "POST", `${TABLE_PATH}/preview`)).toHaveLength(1);
  });
});
