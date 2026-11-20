import { render, screen, within } from "@testing-library/react";
import i18next from "i18next";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { RunOut } from "../api/jobs";
import { aJobRun } from "../test/jobs";
import { RunRow } from "./RunRow";

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * Renders one row in a table.
 *
 * @param run - The run.
 * @returns The row.
 */
function renderRow(run: RunOut): HTMLElement {
  render(
    <MemoryRouter>
      <table>
        <tbody>
          <RunRow run={run} />
        </tbody>
      </table>
    </MemoryRouter>,
  );
  return screen.getByRole("row");
}

describe("RunRow", () => {
  it("shows glyph, word, name, environment, trigger, time, duration and task counts", () => {
    const run = aJobRun({ tasks: [], failed_tasks: 2, total_tasks: 7, status: "failed" });
    const row = renderRow(run);

    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "nightly_sales" })).toHaveAttribute(
      "href",
      `/jobs/prod/runs/${run.id}`,
    );
    expect(row).toHaveTextContent("Environment prod");
    expect(row).toHaveTextContent("Schedule");
    expect(within(row).getByText(when(run.created_at))).toHaveAttribute("dateTime", run.created_at);
    expect(row).toHaveTextContent("38 s");
    expect(row).toHaveTextContent("2 of 7 tasks failed");
    expect(row).toHaveTextContent("›");
  });

  it("counts ended tasks while a run is active, and all of them once none failed", () => {
    const active = renderRow(
      aJobRun({ status: "running", finished_at: null, ended_tasks: 3, total_tasks: 7 }),
    );
    expect(active).toHaveTextContent("3 of 7 tasks");
    active.remove();

    const done = renderRow(aJobRun({ total_tasks: 1 }));
    expect(done).toHaveTextContent("1 task");
    done.remove();

    // Queued: no duration yet.
    const queued = renderRow(
      aJobRun({ status: "queued", started_at: null, finished_at: null, trigger: "api" }),
    );
    expect(queued).toHaveTextContent("API");
    const duration = queued.querySelector("td.number") as HTMLElement;
    expect(within(duration).getByText("–")).toHaveAttribute("aria-hidden", "true");
    expect(within(duration).getByText("Not started yet")).toHaveClass("visually-hidden");
  });

  it("names a run of no job by its repository and says by hand", () => {
    const row = renderRow(aJobRun({ job: null, trigger: null, repository: "team/analytics" }));

    expect(within(row).getByRole("link", { name: "team/analytics" })).toBeInTheDocument();
    expect(row).toHaveTextContent("By hand");
  });

  it("shows a selection runs patterns and a resumes origin", () => {
    const selection = renderRow(
      aJobRun({
        job: null,
        trigger: null,
        kind: "selection",
        selection: { patterns: ["silver.*", 7, "gold.sales"], upstream: true, downstream: true },
      }),
    );
    expect(selection).toHaveTextContent("selection silver.* gold.sales +upstream +downstream");
    selection.remove();

    const bare = renderRow(
      aJobRun({ job: null, kind: "selection", selection: { patterns: "silver.*" } }),
    );
    expect(bare).toHaveTextContent("selection");
    expect(bare).not.toHaveTextContent("+upstream");
    bare.remove();

    const resume = renderRow(
      aJobRun({ kind: "resume", resumed_from_run_id: "0a1b2c3d-4e5f-4061-8293-a4b5c6d7e8f9" }),
    );
    const origin = within(resume).getByRole("link", { name: "resume of 0a1b2c3d" });
    expect(origin).toHaveAttribute("href", "/jobs/prod/runs/0a1b2c3d-4e5f-4061-8293-a4b5c6d7e8f9");
    resume.remove();

    // A job's run is a selection too, and says what started it.
    const ofJob = renderRow(aJobRun({ kind: "selection", trigger: "manual" }));
    expect(ofJob).toHaveTextContent("By hand");
    expect(ofJob).not.toHaveTextContent("selection");
    ofJob.remove();

    // A resume whose origin the server does not name says its trigger.
    const orphan = renderRow(aJobRun({ kind: "resume", resumed_from_run_id: null }));
    expect(orphan).toHaveTextContent("Schedule");
  });
});
