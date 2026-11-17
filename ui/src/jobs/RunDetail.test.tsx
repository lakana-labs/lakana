import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunOut } from "../api/jobs";
import { aFailure, aJobRun, aTask, JOB_RUN_ID } from "../test/jobs";
import { type FakeRoute, fakeServer } from "../test/server";
import { RunDetail } from "./RunDetail";

const RUN = `GET /api/v1/environments/prod/runs/${JOB_RUN_ID}`;

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * Shows where the router is, so a test can read it.
 *
 * @returns The location as text.
 */
function Where() {
  const location = useLocation();
  return <output aria-label="where">{location.pathname + location.search}</output>;
}

/**
 * Renders the run on a job's page, answered by `route`.
 *
 * @param route - What the server answers for the run.
 * @param onRunChanged - Told the run as it changes.
 * @returns The fake server.
 */
function renderRun(route: FakeRoute, onRunChanged = vi.fn()) {
  const server = fakeServer({ [RUN]: route });
  render(
    <MemoryRouter initialEntries={["/jobs/prod/job/analytics/nightly_sales"]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <RunDetail
                environment="prod"
                runId={JOB_RUN_ID}
                level={2}
                onRunChanged={onRunChanged}
              />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  return server;
}

/**
 * Lets the answers in flight arrive.
 */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) {
    await act(async () => {
      await new Promise((resolve) => {
        setImmediate(resolve);
      });
    });
  }
}

/**
 * The answer beside a term.
 *
 * @param term - The term's words.
 * @returns The definition.
 */
function fact(term: string): HTMLElement {
  const dd = screen.getByText(term, { selector: "dt" }).nextElementSibling;
  if (!(dd instanceof HTMLElement)) {
    throw new Error(`no answer for ${term}`);
  }
  return dd;
}

/**
 * The row of a task.
 *
 * @param asset - The task's asset.
 * @returns The row.
 */
function row(asset: string): HTMLElement {
  const name = screen.getByRole("link", { name: asset });
  const tr = name.closest("tr");
  if (tr === null) {
    throw new Error(`no row for ${asset}`);
  }
  return tr;
}

const FAILED: RunOut = aJobRun({
  status: "failed",
  failed_tasks: 2,
  total_tasks: 5,
  ended_tasks: 5,
  actions: ["resume"],
  tasks: [
    aTask(),
    aTask({
      asset: "gold.\u202Esales",
      status: "failed",
      attempt: 3,
      max_attempts: 3,
      finished_at: "2026-11-17T01:00:40Z",
      rows_written: null,
      failure: aFailure("memory"),
    }),
    aTask({
      asset: "gold.margin",
      status: "failed",
      max_attempts: 2,
      finished_at: "2026-11-17T01:00:36Z",
      rows_written: null,
      failure: aFailure("memory_spike"),
    }),
    aTask({
      asset: "gold.ranking",
      status: "upstream_failed",
      started_at: null,
      finished_at: null,
      rows_written: null,
      failure: aFailure("upstream_failed"),
    }),
    aTask({
      asset: "gold.retried",
      attempt: 2,
      max_attempts: 3,
      rows_written: 2_500_000,
    }),
  ],
});

afterEach(() => {
  vi.useRealTimers();
});

describe("RunDetail", () => {
  it("shows the run's facts and its tasks with glyph, word, duration, rows and attempts", async () => {
    renderRun({ status: 200, body: FAILED });

    expect(
      await screen.findByRole("heading", {
        level: 2,
        name: `Run of ${when(FAILED.created_at)}`,
      }),
    ).toBeInTheDocument();
    expect(fact("State")).toHaveTextContent("Failed");
    expect(fact("Trigger")).toHaveTextContent("Schedule");
    expect(fact("Started")).toHaveTextContent(when("2026-11-17T01:00:02Z"));
    expect(fact("Ended")).toHaveTextContent(when("2026-11-17T01:00:40Z"));
    expect(fact("Triggered by")).toHaveTextContent("ana.nowak");
    expect(fact("Commit")).toHaveTextContent(/^4f9d2a7$/);
    expect(screen.queryByText("Tasks", { selector: "dt" })).toBeNull();

    const region = screen.getByRole("region", { name: "Tasks of the run" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(
      within(row("silver.orders"))
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["Succeeded", "silver.orders", "30 s", "1,200", "", "", "›"]);
    expect(row("silver.orders").querySelector(".status-glyph-succeeded")).not.toBeNull();
    expect(screen.getByRole("link", { name: "silver.orders" })).toHaveAttribute(
      "href",
      `/jobs/prod/runs/${JOB_RUN_ID}/tasks/silver.orders`,
    );
    expect(
      within(row("gold.U+202Esales"))
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["Failed", "gold.U+202Esales", "35 s", "", "attempt 3 of 3", "memory", "›"]);
    expect(row("gold.margin")).toHaveTextContent("attempt 1 of 2");
    expect(row("gold.margin")).toHaveTextContent("internal");
    expect(row("gold.ranking")).toHaveTextContent("Upstream failed");
    expect(row("gold.ranking").querySelector(".status-glyph-upstream-failed")).not.toBeNull();
    expect(row("gold.retried")).toHaveTextContent("attempt 2 of 3");
    expect(row("gold.retried")).toHaveTextContent("2.5M");
  });

  it("shows why a pending task waits, and an active run's progress", async () => {
    renderRun({
      status: 200,
      body: aJobRun({
        status: "running",
        finished_at: null,
        started_at: new Date(Date.now() - 42_000).toISOString(),
        total_tasks: 4,
        ended_tasks: 1,
        tasks: [
          aTask({
            asset: "gold.sales",
            status: "pending",
            started_at: null,
            finished_at: null,
            rows_written: null,
            waiting_for: { code: "upstream", message: "It waits for \u202Esilver.orders." },
          }),
        ],
      }),
    });

    expect(await screen.findByText("1 of 4 tasks")).toBeInTheDocument();
    expect(fact("State")).toHaveTextContent("Running");
    expect(fact("Ended")).toHaveTextContent(/^running for 4\d s$/);
    expect(row("gold.sales")).toHaveTextContent("Queued");
    expect(row("gold.sales")).toHaveTextContent("It waits for U+202Esilver.orders.");
  });

  it("shows a run-level failure as its why when no task failed", async () => {
    renderRun({
      status: 200,
      body: aJobRun({
        status: "failed",
        tasks: [],
        error_message: "The project could not be built: \u202Elakana.yaml is not valid.",
      }),
    });

    expect(
      await screen.findByText(
        "Why it failed: The project could not be built: U+202Elakana.yaml is not valid.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("This run has no tasks.")).toBeInTheDocument();
  });

  it("does not show the run's message as its why when a task failed", async () => {
    renderRun({ status: 200, body: { ...FAILED, error_message: "2 tasks failed." } });

    await screen.findByRole("region", { name: "Tasks of the run" });
    expect(screen.queryByText(/Why it failed/)).toBeNull();
  });

  it("offers no control the actions do not list", async () => {
    renderRun({ status: 200, body: { ...FAILED, actions: [] } });

    expect(await screen.findByRole("link", { name: "Logs" })).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("offers Cancel for a running run the actions list, and no Resume", async () => {
    const active = aJobRun({ status: "running", finished_at: null, actions: ["cancel", "resume"] });
    renderRun({ status: 200, body: active });

    expect(await screen.findByRole("button", { name: "Cancel run" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  });

  it("says a queued run has not started, and Cancelling… as soon as its cancel is asked for", async () => {
    const queued = aJobRun({
      status: "queued",
      started_at: null,
      finished_at: null,
      actions: ["cancel"],
      tasks: [],
    });
    const server = renderRun({ status: 200, body: queued });
    server.route({
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/cancel`]: {
        status: 200,
        body: { ...queued, cancel_requested: true },
      },
    });

    expect(await screen.findByText("Not started yet")).toBeInTheDocument();
    expect(screen.queryByText("Ended", { selector: "dt" })).toBeNull();
    (await screen.findByRole("button", { name: "Cancel run" })).click();
    const dialog = await screen.findByRole("dialog");
    within(dialog).getByRole("button", { name: "Cancel run" }).click();

    expect(await screen.findByText("Cancelling…")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says Cancelling… for a run whose cancel was asked for", async () => {
    renderRun({
      status: 200,
      body: aJobRun({
        status: "running",
        finished_at: null,
        cancel_requested: true,
        actions: ["cancel"],
      }),
    });

    expect(await screen.findByText("Cancelling…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
  });

  it("reads a running run every two seconds and stops reading the run once it ended", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const answers = [
      aJobRun({ status: "running", finished_at: null }),
      aJobRun({ status: "running", finished_at: null }),
      aJobRun(),
    ];
    const onRunChanged = vi.fn<(run: RunOut) => void>();
    const server = renderRun(
      () => ({ status: 200, body: answers.shift() ?? aJobRun() }),
      onRunChanged,
    );
    const reads = () => server.sent.filter((request) => request.method === "GET").length;

    await settle();
    expect(reads()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_999);
    });
    await settle();
    expect(reads()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    await settle();
    expect(reads()).toBe(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await settle();
    expect(reads()).toBe(3);
    expect(screen.getByText("Succeeded", { selector: "dd *" })).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    await settle();
    expect(reads()).toBe(3);
    expect(vi.getTimerCount()).toBe(0);
    expect(onRunChanged.mock.calls.map(([run]) => run.status)).toEqual(["running", "succeeded"]);
  });

  it("Logs opens the first failed task", async () => {
    renderRun({ status: 200, body: FAILED });

    expect(await screen.findByRole("link", { name: "Logs" })).toHaveAttribute(
      "href",
      `/jobs/prod/runs/${JOB_RUN_ID}/tasks/gold.margin`,
    );
  });

  it("Logs takes the asset first when two tasks failed at once, and is absent without a failure", async () => {
    const at = "2026-11-17T01:00:40Z";
    renderRun({
      status: 200,
      body: {
        ...FAILED,
        tasks: [
          aTask({ asset: "gold.b", status: "failed", finished_at: at }),
          aTask({ asset: "gold.a", status: "failed", finished_at: at }),
        ],
      },
    });

    expect(await screen.findByRole("link", { name: "Logs" })).toHaveAttribute(
      "href",
      `/jobs/prod/runs/${JOB_RUN_ID}/tasks/gold.a`,
    );
  });

  it("Resume on a job's page selects the new run", async () => {
    const server = renderRun({ status: 200, body: FAILED });
    server.route({
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/resume`]: {
        status: 201,
        body: aJobRun({ id: "r9", status: "queued" }),
      },
    });

    (await screen.findByRole("button", { name: "Resume" })).click();

    await vi.waitFor(() => {
      expect(screen.getByLabelText("where")).toHaveTextContent(
        "/jobs/prod/job/analytics/nightly_sales?run=r9",
      );
    });
  });

  it("gives the focus to the run's heading when Cancel answers with a run that already ended", async () => {
    const active = aJobRun({ status: "running", finished_at: null, actions: ["cancel"] });
    const server = renderRun({ status: 200, body: active });
    server.route({
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/cancel`]: {
        status: 200,
        body: aJobRun({ status: "cancelled", cancel_requested: true }),
      },
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }),
    );

    const heading = screen.getByRole("heading", { level: 2 });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(screen.queryByText("Cancelling…")).toBeNull();
  });

  it("gives the focus to the run's heading when Cancelling… goes as the run ends", async () => {
    let cancelled = false;
    const active = aJobRun({ status: "running", finished_at: null, actions: ["cancel"] });
    const server = renderRun(() => ({
      status: 200,
      body: cancelled ? aJobRun({ status: "cancelled", cancel_requested: true }) : active,
    }));
    server.route({
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/cancel`]: () => {
        cancelled = true;
        return { status: 200, body: { ...active, cancel_requested: true } };
      },
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }),
    );
    await waitFor(() => {
      expect(screen.getByText("Cancelling…").closest(".jobs-control")).toHaveFocus();
    });

    const heading = screen.getByRole("heading", { level: 2 });
    await waitFor(
      () => {
        expect(heading).toHaveFocus();
      },
      { timeout: 5_000 },
    );
    expect(screen.queryByText("Cancelling…")).toBeNull();
    expect(fact("State")).toHaveTextContent("Cancelled");
  });

  it("takes no focus from a person who moved it away while Cancelling… shows", async () => {
    const active = aJobRun({ status: "running", finished_at: null, actions: ["cancel"] });
    let cancelled = false;
    const server = renderRun(() => ({
      status: 200,
      body: { ...active, cancel_requested: cancelled },
    }));
    server.route({
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/cancel`]: () => {
        cancelled = true;
        return { status: 200, body: { ...active, cancel_requested: true } };
      },
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }),
    );
    const line = await screen.findByText("Cancelling…");
    await waitFor(() => {
      expect(line.closest(".jobs-control")).toHaveFocus();
    });
    const reads = server.sent.filter((request) => request.method === "GET").length;

    // A click on a blank part of the page.
    act(() => {
      (document.activeElement as HTMLElement).blur();
    });
    await waitFor(
      () => {
        expect(server.sent.filter((request) => request.method === "GET").length).toBe(reads + 1);
      },
      { timeout: 5_000 },
    );
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });

    expect(document.body).toHaveFocus();
  });

  it("forgets a control the person left for another", async () => {
    const active = aJobRun({ status: "running", finished_at: null, actions: ["cancel"] });
    renderRun({ status: 200, body: active });
    const cancel = await screen.findByRole("button", { name: "Cancel run" });
    const where = screen.getByLabelText("where");
    where.tabIndex = -1;

    act(() => {
      cancel.focus();
    });
    act(() => {
      where.focus();
    });

    expect(where).toHaveFocus();
  });

  it("keeps the control the focus went to right after leaving another for the page", async () => {
    const active = aJobRun({
      status: "running",
      finished_at: null,
      actions: ["cancel"],
      tasks: [aTask({ asset: "gold.sales", status: "failed" })],
    });
    renderRun({ status: 200, body: active });
    const cancel = await screen.findByRole("button", { name: "Cancel run" });
    const logs = screen.getByRole("link", { name: "Logs" });

    act(() => {
      cancel.focus();
      cancel.blur();
      logs.focus();
    });
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });

    expect(logs).toHaveFocus();
  });

  it("says the server's sentence when the run cannot be read", async () => {
    renderRun({ status: 404, body: { type: "NotFoundError", detail: "No run r1 in prod." } });

    expect(await screen.findByRole("alert")).toHaveTextContent("No run r1 in prod.");
  });
});
