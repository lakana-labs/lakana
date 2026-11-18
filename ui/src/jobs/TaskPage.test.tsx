import { act, screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, describe, expect, it, vi } from "vitest";

import { aFailure, aJobRun, anAttempt, aTask, aTaskLog, JOB_RUN_ID } from "../test/jobs";
import { held } from "../test/pending";
import { renderShell } from "../test/render";

const RUN = `/api/v1/environments/prod/runs/${JOB_RUN_ID}`;
const LOG = `${RUN}/tasks/gold.sales/log`;
const PAGE = `/jobs/prod/runs/${JOB_RUN_ID}/tasks/gold.sales`;

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

const FAILED = aTask({
  asset: "gold.sales",
  status: "failed",
  attempt: 2,
  max_attempts: 2,
  failure: aFailure("build_failed"),
});

/**
 * The hrefs of the breadcrumb's links.
 *
 * @returns The hrefs, in order.
 */
function trail(): (string | null)[] {
  return within(screen.getByRole("navigation", { name: "Breadcrumb" }))
    .getAllByRole("link")
    .map((link) => link.getAttribute("href"));
}

afterEach(() => {
  vi.useRealTimers();
});

describe("TaskPage", () => {
  it("reads the log of an ended run once more and stops when the server cannot finish it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const run = aJobRun({ status: "failed", tasks: [FAILED] });
    const { server } = renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: { status: 200, body: run },
        // The server answers 200, not complete and empty, when its storage failed.
        [`GET ${LOG}`]: { status: 200, body: aTaskLog({ text: "", complete: false }) },
      },
    });
    const reads = (): number =>
      server.sent.filter((request) => request.path.endsWith("/log")).length;

    expect(
      await screen.findByText("The run has ended. This is all of the log that could be read."),
    ).toBeInTheDocument();
    const settled = reads();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(reads()).toBe(settled);
    expect(screen.queryByText("Following the log…")).toBeNull();
  });

  it("leaves out the attempt of a task that has not started", async () => {
    const waiting = aTask({
      asset: "gold.sales",
      status: "pending",
      attempt: 0,
      started_at: null,
      finished_at: null,
    });
    renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: { status: 200, body: aJobRun({ tasks: [waiting] }) },
        [`GET ${LOG}`]: { status: 200, body: aTaskLog() },
      },
    });

    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(screen.queryByText("Attempt")).toBeNull();
  });

  it("has the breadcrumb through the job and the run", async () => {
    const run = aJobRun({ status: "failed", tasks: [FAILED] });
    renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: { status: 200, body: run },
        [`GET ${LOG}`]: {
          status: 200,
          body: aTaskLog({ text: "building gold.sales\n", attempts: [anAttempt()] }),
        },
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "gold.sales" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(trail()).toEqual([
        "/jobs",
        "/jobs?environment=prod",
        "/jobs/prod/job/analytics/nightly_sales",
        `/jobs/prod/runs/${JOB_RUN_ID}`,
      ]);
    });
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(crumbs).getByRole("link", { name: `Run of ${when(run.created_at)}` }),
    ).toBeInTheDocument();

    expect(await screen.findByText(/The task failed with build_failed\./u)).toBeInTheDocument();
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show source" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open table" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold/sales",
    );
    expect(screen.queryByRole("link", { name: "Log" })).toBeNull();
    expect(
      await screen.findByRole("region", { name: "Attempts of gold.sales" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Log of gold.sales" })).toHaveTextContent(
      "building gold.sales",
    );
    expect(document.querySelectorAll(".button-primary")).toHaveLength(0);
  });

  it("leaves the job out of the breadcrumb of a run of no job", async () => {
    const waiting = aTask({
      asset: "gold.sales",
      status: "pending",
      started_at: null,
      finished_at: null,
      waiting_for: { code: "upstream", message: "Waits for \u202Esilver.orders." },
    });
    const run = aJobRun({ job: null, trigger: null, status: "failed", tasks: [waiting] });
    renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: { status: 200, body: run },
        [`GET ${LOG}`]: { status: 200, body: aTaskLog() },
      },
    });

    const name = `Run of ${when(run.created_at)} (analytics)`;
    expect(await screen.findByRole("link", { name })).toHaveAttribute(
      "href",
      `/jobs/prod/runs/${JOB_RUN_ID}`,
    );
    expect(trail()).toEqual(["/jobs", "/jobs?environment=prod", `/jobs/prod/runs/${JOB_RUN_ID}`]);
    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(screen.getByText("Waits").nextElementSibling).toHaveTextContent(
      "Waits for U+202Esilver.orders.",
    );
  });

  it("an unknown task is not found with the servers sentence", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: { status: 200, body: aJobRun() },
        [`GET ${LOG}`]: {
          status: 404,
          body: { type: "NotFoundError", detail: "No task gold.‮sales in this run." },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No task gold.U+202Esales in this run.",
    );
    expect(screen.getByRole("heading", { level: 1, name: "gold.sales" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Jobs" })).toHaveAttribute("href", "/jobs");
    expect(screen.queryByRole("region", { name: "Log of gold.sales" })).toBeNull();
  });

  it("an unknown run says so once, with the servers sentence", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: {
          status: 404,
          body: { type: "NotFoundError", detail: `No run ${JOB_RUN_ID} in prod.` },
        },
        [`GET ${LOG}`]: {
          status: 404,
          body: { type: "NotFoundError", detail: `No run ${JOB_RUN_ID} in prod.` },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(`No run ${JOB_RUN_ID} in prod.`);
    await waitFor(() => {
      expect(screen.getAllByRole("alert")).toHaveLength(1);
    });
    expect(screen.getByRole("link", { name: "Run" })).toHaveAttribute(
      "href",
      `/jobs/prod/runs/${JOB_RUN_ID}`,
    );
  });

  it("says it is loading the task after a second", async () => {
    const answer = held();
    renderShell(PAGE, {
      routes: { [`GET ${RUN}`]: answer.route, [`GET ${LOG}`]: { status: 200, body: aTaskLog() } },
    });

    expect(await screen.findByText("Loading the task…")).toHaveAttribute("role", "status");
    answer.release({ status: 200, body: aJobRun({ tasks: [aTask({ asset: "gold.sales" })] }) });
    await waitFor(() => {
      expect(screen.queryByText("Loading the task…")).toBeNull();
    });
  });

  it("a run that cannot be read says so with Try again and still follows the log", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: {
          status: 500,
          body: { type: "InternalError", detail: "The database is down." },
        },
        [`GET ${LOG}`]: { status: 200, body: aTaskLog() },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The database is down.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText("Loading the task…")).toBeNull();
    expect(await screen.findByRole("region", { name: "Log of gold.sales" })).toBeInTheDocument();
  });
});
