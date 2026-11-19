import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { areasFor } from "../shell/areas";
import { DETAIL, HEALTHY } from "../test/home";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";

const PATH = "/settings/status";

/** A worker as the server lists it: alive, idle, on the server's version. */
const WORKER = {
  id: "w-1",
  host: "worker-1.internal",
  version: "0.23.0",
  started_at: "2026-11-18T07:00:00Z",
  heartbeat_at: "2026-11-18T09:00:00Z",
  status: "alive",
  takes: "all",
  query_run_id: null,
  deploy_run_id: null,
  task_run_id: null,
  memory: {
    used_bytes: 1.2 * 2 ** 30,
    limit_bytes: 4 * 2 ** 30,
    measured_at: "2026-11-18T09:00:00Z",
  },
  replaced: false,
};

const SCHEDULER = {
  holder: "lakana-server-1",
  heartbeat_at: "2026-11-18T09:00:00Z",
  active_runs: 2,
  pending: 1,
  ready: 4,
  running: 3,
};

/**
 * Renders Status for a person who may not see the detail.
 *
 * @param routes - The server's answers beyond Home's.
 * @returns The rendered shell.
 */
function asReader(routes: Record<string, FakeRoute>): ReturnType<typeof renderShell> {
  return renderShell(PATH, { access: areasFor(false), me: { name: "kim", admin: false }, routes });
}

/**
 * Renders Status for an administrator, with the detail and the scheduler.
 *
 * @param detail - The status's detail.
 * @param scheduler - What the scheduler answers.
 * @returns The rendered shell.
 */
function asManager(
  detail: unknown,
  scheduler: unknown = SCHEDULER,
): ReturnType<typeof renderShell> {
  return renderShell(PATH, {
    routes: {
      "GET /api/v1/status": { status: 200, body: { ...HEALTHY, detail } },
      "GET /api/v1/scheduler": { status: 200, body: scheduler },
    },
  });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("StatusPage", () => {
  it("shows health, workers and version to everyone", async () => {
    asReader({});

    expect(await screen.findByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText("Everything works.")).toBeInTheDocument();
    expect(screen.getByText("2 workers up")).toBeInTheDocument();
    expect(await screen.findByText("This server runs Lakana 0.23.0.")).toBeInTheDocument();
  });

  it("shows no detail and asks for no scheduler without it", async () => {
    const { server } = asReader({});

    await screen.findByText("Healthy");
    expect(screen.queryByRole("heading", { name: "Workers" })).not.toBeInTheDocument();
    expect(screen.queryByRole("meter")).not.toBeInTheDocument();
    expect(server.sent.filter((request) => request.path === "/api/v1/scheduler")).toEqual([]);
  });

  it("shows compute, memory, workers and scheduler to a manager", async () => {
    asManager({
      ...DETAIL,
      workers: [
        {
          ...WORKER,
          id: "w-2",
          host: "worker-2.internal",
          takes: "interactive",
          query_run_id: "q",
        },
        {
          ...WORKER,
          id: "w-3",
          host: "worker-0.internal",
          status: "lost",
          replaced: true,
          version: "0.22.0",
          takes: "pipeline",
          deploy_run_id: "d",
          memory: null,
        },
        { ...WORKER, id: "w-4", host: "worker-3.internal", task_run_id: "t" },
        {
          ...WORKER,
          memory: { ...WORKER.memory, limit_bytes: null },
        },
      ],
    });

    expect(await screen.findByRole("meter", { name: "Compute" })).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Memory" })).toBeInTheDocument();
    const region = screen.getByRole("region", { name: "Workers" });
    const rows = within(region)
      .getAllByRole("row")
      .slice(1)
      .map((row) => row as HTMLTableRowElement);
    expect(rows.map((row) => row.cells[0]?.textContent)).toEqual([
      "worker-0.internal",
      "worker-1.internal",
      "worker-2.internal",
      "worker-3.internal",
    ]);
    expect(rows.map((row) => row.cells[1]?.textContent)).toEqual([
      "lost, replaced",
      "alive",
      "alive",
      "alive",
    ]);
    expect(rows.map((row) => row.cells[2]?.textContent)).toEqual([
      "pipelines",
      "all work",
      "queries",
      "all work",
    ]);
    expect(rows.map((row) => row.cells[3]?.textContent)).toEqual([
      "a deploy",
      "idle",
      "a query",
      "a task",
    ]);
    expect(rows.map((row) => row.cells[4]?.textContent)).toEqual([
      "—",
      "1.2 GiB, no limit",
      "1.2 of 4.0 GiB",
      "1.2 of 4.0 GiB",
    ]);
    await within(region).findByText("differs from the server");
    expect(rows[0]?.cells[5]).toHaveTextContent("0.22.0 differs from the server");
    expect(rows[1]?.cells[5]).toHaveTextContent(/^0\.23\.0$/);
    expect(rows[0]?.cells[6]?.textContent).toMatch(/2026/);
    const scheduler = await screen.findByText("lakana-server-1");
    const facts = scheduler.closest("dl");
    expect(facts).toHaveTextContent("Active runs2");
    expect(facts).toHaveTextContent("Running tasks3");
  });

  it("says when no worker has registered", async () => {
    asManager(
      { ...DETAIL, slots_total: 0, slots_busy: 0, workers: [] },
      { ...SCHEDULER, holder: null, heartbeat_at: null },
    );
    expect(await screen.findByText("No worker has registered yet.")).toBeInTheDocument();
  });

  it("says when no server holds the scheduler", async () => {
    asManager(DETAIL, { ...SCHEDULER, holder: null, heartbeat_at: null });

    const holder = await screen.findByText("no server holds it");
    expect(holder.querySelector(".status-glyph-failed")).not.toBeNull();
    expect(screen.getByText("never")).toBeInTheDocument();
  });

  it("names the command that starts a worker when none is registered", async () => {
    asReader({
      "GET /api/v1/status": {
        status: 200,
        body: { ...HEALTHY, workers_alive: 0, workers_total: 0 },
      },
    });

    expect(
      await screen.findByText("No worker has registered yet. Start one with this command:"),
    ).toBeInTheDocument();
    expect(screen.getByText("lakana worker")).toBeInTheDocument();
  });

  it("counts the lost workers for everyone", async () => {
    asReader({
      "GET /api/v1/status": {
        status: 200,
        body: {
          ...HEALTHY,
          state: "degraded",
          causes: ["worker_lost"],
          workers_alive: 2,
          workers_total: 3,
        },
      },
    });

    expect(await screen.findByText("2 workers up, 1 lost")).toBeInTheDocument();
  });

  it("a failed poll keeps the last answer", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { server } = asReader({});
    await screen.findByText("Healthy");
    server.route({ "GET /api/v1/status": { status: 500, body: { detail: "down" } } });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(
      await screen.findByText(/^Status could not be read\. It shows the last answer from /),
    ).toBeInTheDocument();
    expect(screen.getByText("Healthy")).toBeInTheDocument();
    server.route({ "GET /api/v1/status": { status: 200, body: HEALTHY } });
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByRole("heading", { level: 1, name: "Status" })).toHaveFocus();
  });

  it("a first read that fails shows the failure alone", async () => {
    asReader({ "GET /api/v1/status": { status: 500, body: { detail: "The database is down." } } });

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Health" })).not.toBeInTheDocument();
  });

  it("a new state does not move the focus", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { server } = asManager({ ...DETAIL, workers: [WORKER] });
    const region = await screen.findByRole("region", { name: "Workers" });
    region.focus();
    server.route({
      "GET /api/v1/status": {
        status: 200,
        body: {
          ...HEALTHY,
          state: "down",
          causes: ["worker_lost"],
          workers_alive: 0,
          workers_total: 1,
          detail: { ...DETAIL, workers: [{ ...WORKER, status: "lost" }] },
        },
      },
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(await screen.findByText("Down")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Workers" })).toHaveFocus();
  });

  it("marks a worker's host", async () => {
    asManager({ ...DETAIL, workers: [{ ...WORKER, host: "build​-01" }] });

    const region = await screen.findByRole("region", { name: "Workers" });
    expect(region).toHaveTextContent("buildU+200B-01");
  });

  it("leaves room for the version notice", async () => {
    renderShell(PATH, {
      routes: { "GET /api/v1/info": { status: 500, body: { detail: "down" } } },
    });

    expect(await screen.findByText("This server runs a development build.")).toBeInTheDocument();
    const slot = document.querySelector('[data-slot="version-notice"]');
    expect(slot).not.toBeNull();
    expect(slot).toBeEmptyDOMElement();
  });
});
