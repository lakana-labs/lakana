import { describe, expect, it } from "vitest";

import {
  ACTIVE_RUN_STATUSES,
  cancelRun,
  getJob,
  getRun,
  getTaskLog,
  listJobRuns,
  pauseJob,
  resumeJob,
  resumeRun,
  showCheckRows,
  startJob,
} from "./jobs";
import type { TaskAttemptOut, TriggerOut, WaitingOut } from "./jobs";

import { aFailure, aJob, aJobRun, aTask, aTaskLog, JOB_RUN_ID } from "../test/jobs";
import { fakeServer } from "../test/server";

const JOBS = "/api/v1/environments/prod/jobs/nightly_sales";
const NO_FILTER = { status: null, since: null };

describe("the job calls", () => {
  it("reads a job of a repository with the repository as a parameter", async () => {
    const server = fakeServer({
      [`GET ${JOBS}`]: { status: 200, body: aJob() },
    });

    expect(await getJob("prod", "analytics", "nightly_sales")).toEqual(aJob());
    expect(server.sent[0]?.path).toBe(`${JOBS}?repository=analytics`);
  });

  it("lists a job's runs with status, since, limit 50 and the cursor from X-Next-Cursor", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod/runs": (request) =>
        request.path.includes("cursor=c1")
          ? { status: 200, body: [] }
          : { status: 200, body: [aJobRun()], headers: { "X-Next-Cursor": "c1" } },
    });

    const first = await listJobRuns("prod", "analytics", "nightly_sales", NO_FILTER, null);
    const second = await listJobRuns(
      "prod",
      "analytics",
      "nightly_sales",
      { status: "failed", since: "2026-11-10T00:00:00Z" },
      first.nextCursor,
    );

    expect(first).toEqual({ items: [aJobRun()], nextCursor: "c1" });
    expect(second).toEqual({ items: [], nextCursor: null });
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/prod/runs?job=nightly_sales&repository=analytics&limit=50",
      "/api/v1/environments/prod/runs?job=nightly_sales&repository=analytics&status=failed&since=2026-11-10T00%3A00%3A00Z&limit=50&cursor=c1",
    ]);
  });

  it("encodes the environment, the job, the run and the asset", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/my env/jobs/a/b": { status: 200, body: aJob() },
      "GET /api/v1/environments/my env/runs/x/y": { status: 200, body: aJobRun() },
      "GET /api/v1/environments/my env/runs/r1/tasks/silver.orders/log": {
        status: 200,
        body: aTaskLog(),
      },
      "GET /api/v1/environments/my env/runs/r1/tasks/a/b/log": {
        status: 200,
        body: aTaskLog(),
      },
    });

    await getJob("my env", "r", "a/b");
    await getRun("my env", "x/y");
    await getTaskLog("my env", "r1", "silver.orders", 0);
    await getTaskLog("my env", "r1", "a/b", 0);

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/my%20env/jobs/a%2Fb?repository=r",
      "/api/v1/environments/my%20env/runs/x%2Fy",
      "/api/v1/environments/my%20env/runs/r1/tasks/silver.orders/log?offset=0",
      "/api/v1/environments/my%20env/runs/r1/tasks/a%2Fb/log?offset=0",
    ]);
  });

  it("starts, pauses and resumes a job with the repository in the body", async () => {
    const started = { run: aJobRun(), waiting: null };
    const server = fakeServer({
      [`POST ${JOBS}/runs`]: { status: 201, body: started },
      [`POST ${JOBS}/pause`]: { status: 200, body: aJob({ paused: true }) },
      [`POST ${JOBS}/resume`]: { status: 200, body: aJob() },
    });

    expect(await startJob("prod", "analytics", "nightly_sales")).toEqual(started);
    expect((await pauseJob("prod", "analytics", "nightly_sales")).paused).toBe(true);
    expect((await resumeJob("prod", "analytics", "nightly_sales")).paused).toBe(false);
    expect(server.sent.map((request) => [request.method, request.body])).toEqual([
      ["POST", { repository: "analytics" }],
      ["POST", { repository: "analytics" }],
      ["POST", { repository: "analytics" }],
    ]);
  });

  it("reads a waiting start as the waiting note", async () => {
    const waiting = { waits_for_run_id: JOB_RUN_ID, message: "It starts when the run ends." };
    fakeServer({ [`POST ${JOBS}/runs`]: { status: 202, body: { run: null, waiting } } });

    expect(await startJob("prod", "analytics", "nightly_sales")).toEqual({ run: null, waiting });
  });

  it("cancels and resumes a run", async () => {
    const server = fakeServer({
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/cancel`]: {
        status: 200,
        body: aJobRun({ cancel_requested: true }),
      },
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/resume`]: {
        status: 202,
        body: aJobRun({ kind: "resume" }),
      },
    });

    expect((await cancelRun("prod", JOB_RUN_ID)).cancel_requested).toBe(true);
    expect((await resumeRun("prod", JOB_RUN_ID)).kind).toBe("resume");
    expect(server.sent.map((request) => request.method)).toEqual(["POST", "POST"]);
  });

  it("reads a log piece at an offset", async () => {
    const piece = aTaskLog({ next_offset: 99, more: true, complete: false });
    const server = fakeServer({
      [`GET /api/v1/environments/prod/runs/${JOB_RUN_ID}/tasks/silver.orders/log`]: {
        status: 200,
        body: piece,
      },
    });

    expect(await getTaskLog("prod", JOB_RUN_ID, "silver.orders", 42)).toEqual(piece);
    expect(server.sent).toHaveLength(1);
  });

  it("asks for a check's rows and returns the query id", async () => {
    const server = fakeServer({
      [`POST /api/v1/environments/prod/runs/${JOB_RUN_ID}/checks/rows`]: {
        status: 202,
        body: { query_id: "q-1" },
      },
    });

    expect(await showCheckRows("prod", JOB_RUN_ID, "silver.orders/no_nulls")).toBe("q-1");
    expect(server.sent[0]?.body).toEqual({ check: "silver.orders/no_nulls" });
  });

  it("reads a page of runs in the background only when asked, as a refresh is", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod/runs": { status: 200, body: [] },
    });

    await listJobRuns("prod", "analytics", "nightly_sales", NO_FILTER, null, { background: true });
    await listJobRuns("prod", "analytics", "nightly_sales", NO_FILTER, "c1");

    expect(server.sent.map((request) => request.headers["x-lakana-background"])).toEqual([
      "1",
      undefined,
    ]);
  });

  it("polls are background requests", async () => {
    const server = fakeServer({
      [`GET ${JOBS}`]: { status: 200, body: aJob() },
      [`GET /api/v1/environments/prod/runs/${JOB_RUN_ID}`]: { status: 200, body: aJobRun() },
      [`GET /api/v1/environments/prod/runs/${JOB_RUN_ID}/tasks/silver.orders/log`]: {
        status: 200,
        body: aTaskLog(),
      },
    });

    await getJob("prod", "analytics", "nightly_sales");
    await getRun("prod", JOB_RUN_ID);
    await getTaskLog("prod", JOB_RUN_ID, "silver.orders", 0);

    expect(server.sent.map((request) => request.headers["x-lakana-background"])).toEqual([
      "1",
      "1",
      "1",
    ]);
  });

  it("names the statuses of a run that is not over", () => {
    expect(ACTIVE_RUN_STATUSES).toEqual(["queued", "running"]);
  });

  it("reads a failed run with its checks and its waiting task, a deploy trigger and attempts", async () => {
    const check = {
      name: "silver.orders/no_nulls",
      table: "silver.orders",
      outcome: "failed" as const,
      failing_rows: 3,
    };
    const waiting: WaitingOut = { code: "upstream", message: "It waits for silver.orders." };
    const attempt: TaskAttemptOut = {
      attempt: 1,
      worker: "w1",
      started_at: "2026-11-17T01:00:05Z",
      finished_at: "2026-11-17T01:00:35Z",
      outcome: "failed",
      rows_written: null,
      error_message: null,
      failure: aFailure("check_blocked"),
      peak_memory_bytes: null,
      cpu_seconds: null,
      bytes_read: null,
    };
    const trigger: TriggerOut = { kind: "deploy" };
    const run = aJobRun({
      status: "failed",
      failed_tasks: 1,
      tasks: [
        aTask({ status: "failed", failure: aFailure("check_blocked"), checks: [check] }),
        aTask({ asset: "gold.sales", status: "pending", waiting_for: waiting }),
      ],
    });
    fakeServer({
      [`GET /api/v1/environments/prod/runs/${JOB_RUN_ID}`]: { status: 200, body: run },
      [`GET ${JOBS}`]: { status: 200, body: aJob({ triggers: [trigger] }) },
      [`GET /api/v1/environments/prod/runs/${JOB_RUN_ID}/tasks/silver.orders/log`]: {
        status: 200,
        body: aTaskLog({ attempts: [attempt] }),
      },
    });

    expect(await getRun("prod", JOB_RUN_ID)).toEqual(run);
    expect((await getJob("prod", "analytics", "nightly_sales")).triggers).toEqual([trigger]);
    expect((await getTaskLog("prod", JOB_RUN_ID, "silver.orders", 0)).attempts).toEqual([attempt]);
  });
});
