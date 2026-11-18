import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JSX } from "react";
import { describe, expect, it, vi } from "vitest";

import type { ConnectionOut, ConnectionQueuedTestOut } from "../api";
import { aConnection } from "../test/connections";
import { held } from "../test/pending";
import { fakeServer, type FakeRoute } from "../test/server";
import { TestAll } from "./TestAll";
import { FollowedAnswers, testKey } from "./useOpenTests";

const TEST_ALL = "POST /api/v1/environments/dev/connection-tests";

/**
 * What Test all queued for a connection.
 *
 * @param connection - The connection's name.
 * @returns The item.
 */
function item(connection: string): ConnectionQueuedTestOut {
  return { connection, test_id: `t-${connection}`, queued: true };
}

/**
 * Test all's route, a page per cursor.
 *
 * @param pages - The pages by cursor; "" for the first.
 * @returns The route.
 */
function byCursor(
  pages: Record<string, { items: ConnectionQueuedTestOut[]; next_cursor: string | null }>,
): FakeRoute {
  return (request) => {
    const cursor = new URL(request.path, "http://localhost").searchParams.get("cursor") ?? "";
    return { status: 200, body: pages[cursor] };
  };
}

/**
 * Test all in `dev`, with the connections the screen has read.
 *
 * @param props - The answers and the callback.
 * @param props.answers - The connections read, by key.
 * @param props.onQueued - Called with what was queued.
 * @param props.stuck - The keys whose test is stuck.
 * @param props.gone - The keys of connections found deleted.
 * @param props.failed - Whether following stopped after failed reads.
 * @returns The button with its lines.
 */
function InDev({
  answers,
  onQueued = () => undefined,
  stuck = new Set(),
  gone = new Set(),
  failed = false,
}: {
  readonly answers: ReadonlyMap<string, ConnectionOut>;
  readonly onQueued?: (items: readonly ConnectionQueuedTestOut[]) => void;
  readonly stuck?: ReadonlySet<string>;
  readonly gone?: ReadonlySet<string>;
  readonly failed?: boolean;
}): JSX.Element {
  return (
    <FollowedAnswers value={{ answers, stuck, gone, failed }}>
      <TestAll environment="dev" onQueued={onQueued} />
    </FollowedAnswers>
  );
}

/**
 * The connections read, by key, all in `dev`.
 *
 * @param rows - The connections.
 * @returns The map.
 */
function read(...rows: ConnectionOut[]): ReadonlyMap<string, ConnectionOut> {
  return new Map(rows.map((row) => [testKey("dev", row.name), row]));
}

/**
 * A connection of `dev` whose test is done.
 *
 * @param name - Its name.
 * @param outcome - What the test found.
 * @returns The connection.
 */
function done(name: string, outcome: "ok" | "refused"): ConnectionOut {
  return aConnection({
    name,
    environment: "dev",
    last_test: { state: "done", outcome, finished_at: "2026-11-17T12:00:00Z" },
  });
}

describe("TestAll", () => {
  it("queues every page and says how many it tests", async () => {
    const onQueued = vi.fn();
    const server = fakeServer({
      [TEST_ALL]: byCursor({
        "": { items: [item("a"), item("b")], next_cursor: "c2" },
        c2: { items: [item("c")], next_cursor: null },
      }),
    });
    const user = userEvent.setup();
    render(<InDev answers={new Map()} onQueued={onQueued} />);
    const status = screen.getByRole("status");
    expect(status).toBeEmptyDOMElement();

    await user.click(screen.getByRole("button", { name: "Test all in dev" }));

    await waitFor(() => {
      expect(status).toHaveTextContent("Testing 3 connections in dev…");
    });
    expect(onQueued).toHaveBeenCalledWith([item("a"), item("b"), item("c")]);
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/dev/connection-tests?limit=200",
      "/api/v1/environments/dev/connection-tests?limit=200&cursor=c2",
    ]);
  });

  it("says how many were OK once all are done", async () => {
    fakeServer({
      [TEST_ALL]: byCursor({ "": { items: [item("a"), item("b"), item("c")], next_cursor: null } }),
    });
    const user = userEvent.setup();
    const { rerender } = render(<InDev answers={new Map()} />);
    await user.click(screen.getByRole("button", { name: "Test all in dev" }));
    const status = screen.getByRole("status");
    await waitFor(() => {
      expect(status).toHaveTextContent("Testing 3 connections in dev…");
    });

    rerender(<InDev answers={read(done("a", "ok"), done("b", "refused"))} />);
    expect(status).toHaveTextContent("Testing 3 connections in dev…");

    rerender(<InDev answers={read(done("a", "ok"), done("b", "refused"), done("c", "ok"))} />);
    expect(status).toHaveTextContent("Tested 3 connections in dev: 2 OK, 1 not OK.");
  });

  it("says it stopped following once the reads failed or every open test is stuck", async () => {
    fakeServer({
      [TEST_ALL]: byCursor({ "": { items: [item("a"), item("b"), item("c")], next_cursor: null } }),
    });
    const user = userEvent.setup();
    const answers = read(done("a", "ok"));
    const { rerender } = render(<InDev answers={answers} />);
    await user.click(screen.getByRole("button", { name: "Test all in dev" }));
    const status = screen.getByRole("status");
    await waitFor(() => {
      expect(status).toHaveTextContent("Testing 3 connections in dev…");
    });

    // One open test is stuck, another still followed: the line still waits.
    rerender(<InDev answers={answers} stuck={new Set([testKey("dev", "b")])} />);
    expect(status).toHaveTextContent("Testing 3 connections in dev…");

    rerender(
      <InDev answers={answers} stuck={new Set([testKey("dev", "b"), testKey("dev", "c")])} />,
    );
    expect(status).toHaveTextContent("Stopped following 2 tests in dev; see the rows.");

    // A deleted connection's test ends with no outcome, like a stuck one.
    rerender(
      <InDev
        answers={answers}
        stuck={new Set([testKey("dev", "b")])}
        gone={new Set([testKey("dev", "c")])}
      />,
    );
    expect(status).toHaveTextContent("Stopped following 2 tests in dev; see the rows.");
    rerender(
      <InDev
        answers={read(done("a", "ok"), done("b", "ok"))}
        gone={new Set([testKey("dev", "c")])}
      />,
    );
    expect(status).toHaveTextContent("Stopped following 1 test in dev; see the rows.");

    rerender(<InDev answers={read(done("a", "ok"), done("b", "ok"))} failed />);
    expect(status).toHaveTextContent("Stopped following 1 test in dev; see the rows.");
  });

  it("words one connection in the singular", async () => {
    fakeServer({ [TEST_ALL]: byCursor({ "": { items: [item("a")], next_cursor: null } }) });
    const user = userEvent.setup();
    const { rerender } = render(<InDev answers={new Map()} />);
    await user.click(screen.getByRole("button", { name: "Test all in dev" }));
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Testing 1 connection in dev…");
    });

    rerender(<InDev answers={read(done("a", "ok"))} />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Tested 1 connection in dev: 1 OK, 0 not OK.",
    );
  });

  it("shows the server's refusal beside the button", async () => {
    const answer = held();
    fakeServer({ [TEST_ALL]: answer.route });
    const user = userEvent.setup();
    const onQueued = vi.fn();
    render(<InDev answers={new Map()} onQueued={onQueued} />);

    await user.click(screen.getByRole("button", { name: "Test all in dev" }));
    answer.release({
      status: 403,
      body: { detail: "You may not test connections in dev.", type: "Forbidden" },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You may not test connections in dev.",
    );
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(onQueued).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Test all in dev" })).toBeEnabled();
  });
});
