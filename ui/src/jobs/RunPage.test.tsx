import { screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { describe, expect, it } from "vitest";

import { aJobRun, aTask, JOB_RUN_ID } from "../test/jobs";
import { held } from "../test/pending";
import { renderShell } from "../test/render";

const RUN = `/api/v1/environments/prod/runs/${JOB_RUN_ID}`;
const PAGE = `/jobs/prod/runs/${JOB_RUN_ID}`;

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

describe("RunPage", () => {
  it("has the breadcrumb through the job", async () => {
    const run = aJobRun({ job: "nightly_\u202Esales", repository: "team/analytics" });
    renderShell(PAGE, { routes: { [`GET ${RUN}`]: { status: 200, body: run } } });

    const title = `Run of ${when(run.created_at)}`;
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    await waitFor(() => {
      expect(document.title).toBe(`${title} · Lakana`);
    });
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(trail)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")),
    ).toEqual(["/jobs", "/jobs", "/jobs/prod/job/team%2Fanalytics/nightly_%E2%80%AEsales"]);
    expect(within(trail).getByRole("link", { name: "nightly_U+202Esales" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
    expect(document.querySelectorAll(".button-primary")).toHaveLength(0);
  });

  it("names the repository in the title of a run of no job", async () => {
    const run = aJobRun({ job: null, trigger: null, repository: "\u202Eanalytics" });
    renderShell(PAGE, { routes: { [`GET ${RUN}`]: { status: 200, body: run } } });

    const title = `Run of ${when(run.created_at)} (U+202Eanalytics)`;
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getAllByRole("link")).toHaveLength(2);
  });

  it("records the opened run", async () => {
    const { server } = renderShell(PAGE, {
      routes: { [`GET ${RUN}`]: { status: 200, body: aJobRun() } },
    });

    await waitFor(() => {
      expect(
        server.sent.filter((request) => request.path === "/api/v1/principals/admin/recent"),
      ).toHaveLength(1);
    });
    expect(server.sent.find((request) => request.path.endsWith("/recent"))?.body).toEqual({
      kind: "run",
      environment: "prod",
      ref: JOB_RUN_ID,
    });
  });

  it("records a run once, when a cancel changes it and when the record fails", async () => {
    const running = aJobRun({ status: "running", finished_at: null, actions: ["cancel"] });
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: { status: 200, body: running },
        [`POST ${RUN}/cancel`]: { status: 200, body: { ...running, cancel_requested: true } },
        "POST /api/v1/principals/admin/recent": { status: 500 },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }),
    );

    expect(await screen.findByText("Cancelling…")).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.endsWith("/recent"))).toHaveLength(1);
  });

  it("an unknown run is not found with the servers sentence", async () => {
    const { server } = renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: {
          status: 404,
          body: { type: "NotFoundError", detail: `No run ${JOB_RUN_ID} in \u202Eprod.` },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      `No run ${JOB_RUN_ID} in U+202Eprod.`,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Run" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Jobs" })).toHaveAttribute("href", "/jobs");
    expect(server.sent.some((request) => request.path.endsWith("/recent"))).toBe(false);
  });

  it("says it is loading the run after a second", async () => {
    const answer = held();
    renderShell(PAGE, { routes: { [`GET ${RUN}`]: answer.route } });

    expect(await screen.findByText("Loading the run…")).toHaveAttribute("role", "status");
    answer.release({ status: 200, body: aJobRun() });
    await waitFor(() => {
      expect(screen.queryByText("Loading the run…")).toBeNull();
    });
  });

  it("Resume goes to the new run's page", async () => {
    const failed = aJobRun({ status: "failed", actions: ["resume"], failed_tasks: 1 });
    const next = aJobRun({ id: "r9", kind: "resume", status: "queued" });
    const { router, user } = renderShell(PAGE, {
      routes: {
        [`GET ${RUN}`]: {
          status: 200,
          body: { ...failed, tasks: [aTask({ status: "failed" })] },
        },
        [`POST ${RUN}/resume`]: { status: 201, body: next },
        "GET /api/v1/environments/prod/runs/r9": { status: 200, body: next },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Resume" }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/jobs/prod/runs/r9");
    });
    expect(await screen.findByText("Resumed the failed part in a new run.")).toBeInTheDocument();
  });
});
