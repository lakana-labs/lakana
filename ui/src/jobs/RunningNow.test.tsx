import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunOut } from "../api/jobs";
import { aJobRun } from "../test/jobs";
import { type FakeRoute, fakeServer } from "../test/server";
import { RunningNow } from "./RunningNow";

const RUNS = "GET /api/v1/runs";
const MINUTE = 60_000;

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * A running run of `nightly_sales` in `prod`, started 14 minutes ago, 3 of its 7 tasks ended.
 *
 * @param over - The fields that differ.
 * @returns The run.
 */
function running(over: Partial<RunOut> = {}): RunOut {
  return aJobRun({
    id: "r-running",
    status: "running",
    tasks: [],
    created_at: new Date(Date.now() - 15 * MINUTE).toISOString(),
    started_at: new Date(Date.now() - 14 * MINUTE).toISOString(),
    finished_at: null,
    ended_tasks: 3,
    total_tasks: 7,
    actions: ["cancel", "resume"],
    ...over,
  });
}

/**
 * Renders Running now over a server.
 *
 * @param routes - What the server answers.
 * @param query - The search.
 * @param environment - The environment chosen; all by default.
 * @returns The server, the change handler and a user-event session.
 */
function renderRunning(
  routes: Record<string, FakeRoute>,
  query = "",
  environment: string | null = null,
) {
  const server = fakeServer(routes);
  const onChanged = vi.fn();
  const page = (reloadKey: number) => (
    <MemoryRouter>
      <RunningNow
        environment={environment}
        query={query}
        reloadKey={reloadKey}
        onChanged={onChanged}
      />
    </MemoryRouter>
  );
  const view = render(page(0));
  return {
    server,
    onChanged,
    user: userEvent.setup(),
    reload: (key: number) => {
      view.rerender(page(key));
    },
  };
}

/**
 * Moves the fake clock on and lets the timers that fell due run.
 *
 * @param ms - How far to move it.
 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/**
 * Lets the reads and their renders run until `condition` holds, a turn of the event loop at a time.
 * The fake clock stands still meanwhile, and Testing Library's own waits would hang under it (they
 * end by waiting on the faked `setTimeout`), so this waits on the state, never on elapsed time. A
 * state that does not come within a thousand turns fails the test and names what it waited for.
 *
 * @param condition - What must hold.
 * @param what - The condition in words, for the failure.
 */
async function until(condition: () => boolean, what: string): Promise<void> {
  for (let turn = 0; !condition(); turn += 1) {
    if (turn >= 1000) {
      throw new Error(`Still waiting after ${String(turn)} turns for: ${what}`);
    }
    await act(async () => {
      await new Promise((resolve) => {
        setImmediate(resolve);
      });
    });
  }
}

/**
 * Waits until the poll has taken in the answer of its latest read and armed the next one: a read
 * clears its timer while it is open, so one timer standing says the answer is in.
 */
async function nextReadArmed(): Promise<void> {
  await until(() => vi.getTimerCount() === 1, "the next read to be armed (one timer standing)");
}

const page = (items: readonly RunOut[], next: string | null = null): FakeRoute => ({
  status: 200,
  body: { items, next_cursor: next },
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("RunningNow", () => {
  it("lists queued and running runs with glyph, word, name, environment, start, duration and tasks", async () => {
    // The durations read the clock: stand it still so a slow machine cannot move "14 min" on.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-25T10:00:00Z"));
    const run = running();
    const queued = running({
      id: "r-queued",
      job: "weekly_report",
      environment: "dev",
      status: "queued",
      started_at: null,
      ended_tasks: 0,
    });
    const { server } = renderRunning({ [RUNS]: page([run, queued]) });

    expect(
      await screen.findByRole("heading", { level: 2, name: "Running now (2)" }),
    ).toBeInTheDocument();
    const region = screen.getByRole("region", { name: "Runs running now" });
    const [, first, second] = within(region).getAllByRole("row");
    if (first === undefined || second === undefined) {
      throw new Error("two rows expected");
    }
    expect(within(first).getByText("Running")).toBeInTheDocument();
    expect(within(first).getByRole("link", { name: "nightly_sales" })).toHaveAttribute(
      "href",
      "/jobs/prod/runs/r-running",
    );
    expect(first).toHaveTextContent("Environment prod");
    expect(first).toHaveTextContent(
      `started ${when(run.started_at ?? "")} · 14 min · 3 of 7 tasks`,
    );
    expect(first).toHaveTextContent("›");
    expect(within(second).getByText("Queued")).toBeInTheDocument();
    expect(within(second).getByRole("link", { name: "weekly_report" })).toHaveAttribute(
      "href",
      "/jobs/dev/runs/r-queued",
    );
    expect(second).toHaveTextContent(`queued ${when(queued.created_at)}`);
    expect(server.sent[0]?.path).toBe("/api/v1/runs?status=queued%2Crunning&limit=50");
  });

  it("a run of no job is named by its repository", async () => {
    renderRunning({ [RUNS]: page([running({ job: null, trigger: null, repository: "team/ml" })]) });

    expect(await screen.findByRole("link", { name: "team/ml" })).toHaveAttribute(
      "href",
      "/jobs/prod/runs/r-running",
    );
  });

  it("offers Cancel only when the run's actions list it", async () => {
    renderRunning({
      [RUNS]: page([running(), running({ id: "r2", job: "weekly_report", actions: [] })]),
    });

    const cancel = await screen.findByRole("button", { name: "Cancel the run of nightly_sales" });
    expect(cancel).toHaveTextContent("Cancel");
    expect(screen.getAllByRole("button", { name: /^Cancel/u })).toHaveLength(1);
    expect(cancel).not.toHaveClass("button-primary");
  });

  it("a refused cancel shows the servers sentence and reads the section again", async () => {
    const { server, onChanged, user, reload } = renderRunning({
      [RUNS]: page([running()]),
      "POST /api/v1/environments/prod/runs/r-running/cancel": {
        status: 409,
        body: { type: "ConflictError", detail: "The run has already ended." },
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Cancel the run of nightly_sales" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Cancel the run of nightly_sales?");
    await user.click(within(dialog).getByRole("button", { name: "Cancel run" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The run has already ended.");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
    // The list reads again: the run has left it.
    server.route({ [RUNS]: page([]) });
    reload(1);
    expect(await screen.findByText("Nothing is running.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The run has already ended.");
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Running now (0)" })).toHaveFocus();
    });
  });

  it("a refused cancel keeps the focus in the section when the list is read before the dialog hands the focus back", async () => {
    // The dialog gives the focus back to the Cancel button on an animation frame. Hold the frames,
    // as a loaded machine does, so the list is read again and the row is gone before it does.
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
    const { server, user, reload } = renderRunning({
      [RUNS]: page([running()]),
      "POST /api/v1/environments/prod/runs/r-running/cancel": {
        status: 409,
        body: { type: "ConflictError", detail: "The run has already ended." },
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Cancel the run of nightly_sales" }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("The run has already ended.");
    server.route({ [RUNS]: page([]) });
    reload(1);
    expect(await screen.findByText("Nothing is running.")).toBeInTheDocument();
    act(() => {
      for (const frame of frames.splice(0)) {
        try {
          frame(0);
        } catch (error) {
          // The dialog's own frame finds its button gone and its scope too, and React Aria's
          // walker throws on that; any other error is a real one.
          if (!String(error).includes("'currentNode' property on 'TreeWalker'")) {
            throw error;
          }
        }
      }
    });

    expect(screen.getByRole("heading", { level: 2, name: "Running now (0)" })).toHaveFocus();
  });

  it("a cancel asked for shows Cancelling… and reads the list again", async () => {
    const { onChanged, user } = renderRunning({
      [RUNS]: page([running()]),
      "POST /api/v1/environments/prod/runs/r-running/cancel": {
        status: 200,
        body: running({ cancel_requested: true }),
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Cancel the run of nightly_sales" }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }),
    );

    expect(await screen.findByText("Cancelling…")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("says when more runs are running than shown", async () => {
    renderRunning({ [RUNS]: page([running()], "c1") });

    expect(
      await screen.findByRole("heading", { level: 2, name: "Running now (1+)" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/^More runs are running than shown here\./u)).toBeInTheDocument();
  });

  it("the more line opens the Runs tab filtered to running", async () => {
    renderRunning({ [RUNS]: page([running()], "c1") }, "", "prod");

    expect(await screen.findByRole("link", { name: "See every running run" })).toHaveAttribute(
      "href",
      "/jobs/runs?environment=prod&status=queued%2Crunning",
    );
  });

  it("says nothing is running", async () => {
    renderRunning({ [RUNS]: page([]) });

    expect(await screen.findByText("Nothing is running.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Runs running now" })).toBeNull();
  });

  it("says why it could not be read, with Try again", async () => {
    const { server, user } = renderRunning({
      [RUNS]: { status: 503, body: { detail: "The server is starting." } },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/./u);
    server.route({ [RUNS]: page([]) });
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Nothing is running.")).toBeInTheDocument();
  });

  it("keeps the rows through two failed reads and says why after the third", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { server } = renderRunning({ [RUNS]: page([running()]) });
    await until(
      () => screen.queryByRole("link", { name: "nightly_sales" }) !== null,
      "the nightly_sales row to be shown",
    );
    await nextReadArmed();

    server.route({ [RUNS]: { status: 503, body: { detail: "The server is starting." } } });
    // The waits double from 30 s up to 60 s after each failure; a read comes no sooner.
    for (const [wait, reads] of [
      [30_000, 2],
      [60_000, 3],
    ] as const) {
      await advance(wait - 1);
      expect(server.sent).toHaveLength(reads - 1);
      await advance(1);
      await nextReadArmed();
      expect(server.sent).toHaveLength(reads);
      expect(screen.queryByRole("alert")).toBeNull();
    }
    await advance(60_000);
    await until(() => screen.queryByRole("alert") !== null, "the alert to be shown");
    expect(screen.getByRole("alert")).toHaveTextContent(/./u);
    expect(server.sent).toHaveLength(4);
    expect(screen.getByRole("link", { name: "nightly_sales" })).toBeInTheDocument();
  });

  it("reads again every thirty seconds and not while hidden", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let visibility = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(
      () => visibility as DocumentVisibilityState,
    );
    const { server } = renderRunning({ [RUNS]: page([running()]) });
    await until(
      () => screen.queryByRole("link", { name: "nightly_sales" }) !== null,
      "the nightly_sales row to be shown",
    );
    await nextReadArmed();
    expect(server.sent).toHaveLength(1);

    await advance(30_000);
    await nextReadArmed();
    expect(server.sent).toHaveLength(2);

    visibility = "hidden";
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(vi.getTimerCount()).toBe(0);
    await advance(120_000);
    expect(server.sent).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("narrows the runs to the search, by job or repository", async () => {
    const { server } = renderRunning(
      {
        [RUNS]: page([
          running(),
          running({ id: "r2", job: "weekly_report", repository: "finance" }),
        ]),
      },
      "FIN",
    );

    expect(
      await screen.findByRole("heading", { level: 2, name: "Running now (1)" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "weekly_report" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "nightly_sales" })).toBeNull();
    // The search narrows what was read; the server's list is not filtered by it.
    expect(server.sent[0]?.path).not.toContain("FIN");
  });
});

describe("RunningNow with no hit", () => {
  it("says nothing matches the search", async () => {
    renderRunning({ [RUNS]: page([running()]) }, "nothing-like-it");

    expect(await screen.findByText("Nothing here matches the search.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Runs running now" })).toBeNull();
    });
  });
});
