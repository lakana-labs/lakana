import { describe, expect, it } from "vitest";

import {
  ACTIVE_RUN_STATUSES,
  cancelRun,
  getDeploy,
  getJob,
  getRun,
  getTaskLog,
  listAllRuns,
  listDeployments,
  listDeploys,
  listFailedRuns,
  listJobRuns,
  listJobs,
  pauseJob,
  resumeJob,
  resumeRun,
  rollBack,
  showCheckRows,
  startJob,
  undeploy,
} from "./jobs";
import type { TaskAttemptOut, TriggerOut, WaitingOut } from "./jobs";

import {
  aDeploy,
  aDeployment,
  aFailedRun,
  aFailure,
  aJob,
  aJobRun,
  aTask,
  aTaskLog,
  DEPLOY_ID,
  JOB_RUN_ID,
} from "../test/jobs";
import { fakeServer } from "../test/server";

const JOBS = "/api/v1/environments/prod/jobs/nightly_sales";
const NO_FILTER = { status: null, since: null };
const ALL_RUNS = {
  environment: null,
  status: [],
  trigger: [],
  job: null,
  repository: null,
  since: null,
} as const;

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

describe("the Jobs list's calls", () => {
  it("sends a sort to every list of the Jobs area", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod/jobs": { status: 200, body: [] },
      "GET /api/v1/environments/prod/runs": { status: 200, body: [] },
      "GET /api/v1/environments/prod/deploys": { status: 200, body: [] },
      "GET /api/v1/runs": { status: 200, body: { items: [], next_cursor: null } },
    });
    const byState = { sort: "status", order: "asc" } as const;

    await listJobs("prod", undefined, { sort: "last_run", order: "desc" });
    await listJobRuns("prod", "analytics", "nightly", { ...NO_FILTER, sort: byState }, null);
    await listDeploys("prod", null, null, byState);
    await listAllRuns({ ...ALL_RUNS, sort: { sort: "name", order: "desc" } }, null);

    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/prod/jobs?sort=last_run&order=desc",
      "/api/v1/environments/prod/runs?job=nightly&repository=analytics&sort=status&order=asc&limit=50",
      "/api/v1/environments/prod/deploys?sort=status&order=asc&limit=50",
      "/api/v1/runs?sort=name&order=desc&limit=50",
    ]);
  });

  it("lists an environments jobs of one repository", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod/jobs": { status: 200, body: [aJob()] },
      "GET /api/v1/environments/dev/a/jobs": { status: 200, body: [] },
    });

    expect(await listJobs("prod", "analytics")).toEqual([aJob()]);
    expect(await listJobs("dev/a")).toEqual([]);
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/prod/jobs?repository=analytics",
      "/api/v1/environments/dev%2Fa/jobs",
    ]);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });

  it("lists running runs across environments with the status and the cursor from the body", async () => {
    const server = fakeServer({
      "GET /api/v1/runs": (request) =>
        request.path.includes("cursor=c1")
          ? { status: 200, body: { items: [], next_cursor: null } }
          : { status: 200, body: { items: [aJobRun()], next_cursor: "c1" } },
    });

    const first = await listAllRuns({ ...ALL_RUNS, status: ["queued", "running"] }, null);
    const second = await listAllRuns(
      { ...ALL_RUNS, status: ["queued", "running"] },
      first.nextCursor,
      { limit: 10, background: false },
    );

    expect(first).toEqual({ items: [aJobRun()], nextCursor: "c1" });
    expect(second).toEqual({ items: [], nextCursor: null });
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/runs?status=queued%2Crunning&limit=50",
      "/api/v1/runs?status=queued%2Crunning&limit=10&cursor=c1",
    ]);
    expect(server.sent.map((request) => request.headers["x-lakana-background"])).toEqual([
      "1",
      undefined,
    ]);
  });

  it("adds environment, trigger, job, repository and since only when set", async () => {
    const server = fakeServer({
      "GET /api/v1/runs": { status: 200, body: { items: [], next_cursor: null } },
    });

    await listAllRuns(
      {
        environment: "prod",
        status: ["failed"],
        trigger: ["schedule", "manual"],
        job: "nightly_sales",
        repository: "analytics",
        since: "2026-11-10T00:00:00Z",
      },
      null,
    );

    expect(server.sent[0]?.path).toBe(
      "/api/v1/runs?environment=prod&status=failed&trigger=schedule%2Cmanual&job=nightly_sales&repository=analytics&since=2026-11-10T00%3A00%3A00Z&limit=50",
    );
    await listAllRuns(ALL_RUNS, null);
    expect(server.sent[1]?.path).toBe("/api/v1/runs?limit=50");
  });

  it("lists failed runs with and without an environment", async () => {
    const body = { items: [aFailedRun()], truncated: true };
    const server = fakeServer({
      "GET /api/v1/runs/failed": { status: 200, body },
    });

    expect(await listFailedRuns(null)).toEqual(body);
    expect(await listFailedRuns("prod")).toEqual(body);
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/runs/failed",
      "/api/v1/runs/failed?environment=prod",
    ]);
    expect(server.sent[1]?.headers["x-lakana-background"]).toBe("1");
  });
});

describe("the deploy calls", () => {
  it("lists deploys of a repository with the cursor from X-Next-Cursor", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/my env/deploys": (request) =>
        request.path.includes("cursor=")
          ? { status: 200, body: [] }
          : { status: 200, body: [aDeploy()], headers: { "X-Next-Cursor": "d2" } },
    });

    expect(await listDeploys("my env", "team/analytics", null)).toEqual({
      items: [aDeploy()],
      nextCursor: "d2",
    });
    expect(await listDeploys("my env", null, "d2")).toEqual({ items: [], nextCursor: null });
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/my%20env/deploys?repository=team%2Fanalytics&limit=50",
      "/api/v1/environments/my%20env/deploys?limit=50&cursor=d2",
    ]);
  });

  it("reads one deploy with its jobs", async () => {
    const deploy = aDeploy({ jobs: ["daily_sales", "nightly_sales"] });
    const server = fakeServer({
      [`GET /api/v1/environments/prod/deploys/${DEPLOY_ID}`]: { status: 200, body: deploy },
    });

    expect(await getDeploy("prod", DEPLOY_ID)).toEqual(deploy);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
  });

  it("lists the deployments", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod/deployments": { status: 200, body: [aDeployment()] },
    });

    expect(await listDeployments("prod")).toEqual([aDeployment()]);
    expect(server.sent[0]?.path).toBe("/api/v1/environments/prod/deployments");
  });

  it("sends a rollback to a commit", async () => {
    const server = fakeServer({
      "POST /api/v1/environments/prod/deploys": {
        status: 202,
        body: { id: DEPLOY_ID, status: "queued" },
      },
    });

    expect(await rollBack("prod", "analytics", "4f9d2a7c")).toEqual({
      id: DEPLOY_ID,
      status: "queued",
    });
    expect(server.sent[0]?.body).toEqual({
      repository: "analytics",
      kind: "rollback",
      to_sha: "4f9d2a7c",
    });
  });

  it("undeploys a repository from an environment, the names in the path encoded", async () => {
    const server = fakeServer({
      "DELETE /api/v1/environments/a b/deployments/sales ops": { status: 204 },
    });

    await undeploy("a b", "sales ops");

    expect(server.sent.map((each) => [each.method, each.path, each.body])).toEqual([
      ["DELETE", "/api/v1/environments/a%20b/deployments/sales%20ops", undefined],
    ]);
  });
});
