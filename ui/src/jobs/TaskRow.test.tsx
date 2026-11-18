import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import type { TaskRunOut } from "../api/jobs";
import { fakeServer } from "../test/server";
import { aFailure, aJobRun, aTask, JOB_RUN_ID } from "../test/jobs";
import { TaskRow } from "./TaskRow";

/**
 * Renders the task's rows in a table, as the run's tasks list shows them.
 *
 * @param task - The task.
 * @returns The table.
 */
function renderTask(task: TaskRunOut): HTMLElement {
  fakeServer();
  render(
    <MemoryRouter>
      <table>
        <tbody>
          <TaskRow environment="prod" run={aJobRun({ tasks: [task] })} task={task} />
        </tbody>
      </table>
    </MemoryRouter>,
  );
  return screen.getByRole("table");
}

/**
 * The second line of the task, with its controls.
 *
 * @returns The row.
 */
function secondLine(): HTMLElement {
  const second = screen.getAllByRole("row")[1];
  if (second === undefined) {
    throw new Error("the task has no second line");
  }
  return second;
}

const FAILED = aTask({
  asset: "gold.sales",
  status: "failed",
  failure: aFailure("build_failed"),
});

describe("TaskRow", () => {
  it("a failed task shows its failure with Log, Show source and Open table", () => {
    const table = renderTask(FAILED);

    expect(within(table).getAllByRole("row")).toHaveLength(2);
    const line = within(secondLine());
    expect(secondLine()).toHaveTextContent("Cause: The task failed with build_failed.");
    expect(line.getByRole("link", { name: "Log gold.sales" })).toHaveAttribute(
      "href",
      `/jobs/prod/runs/${JOB_RUN_ID}/tasks/gold.sales`,
    );
    // A button among buttons: the Catalog's link-button style is not loaded on the Jobs screen.
    expect(line.getByRole("button", { name: "Show source gold.sales" })).toHaveClass("button");
    expect(line.getByRole("link", { name: "Open table gold.sales" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold/sales",
    );
  });

  it("breaks a task's name after its dots, not inside a word", () => {
    const table = renderTask(FAILED);

    const name = within(table).getByRole("link", { name: "gold.sales" });
    expect(name.querySelectorAll("wbr")).toHaveLength(1);
  });

  it("names each control by its task, so a list of them is not Log, Log, Log", () => {
    renderTask(FAILED);

    const line = within(secondLine());
    expect(line.getByRole("link", { name: "Log gold.sales" })).toHaveTextContent("Log");
    expect(line.getByRole("link", { name: "Open table gold.sales" })).toBeInTheDocument();
    expect(line.getByRole("button", { name: "Show source gold.sales" })).toBeInTheDocument();
  });

  it("a task that only checks a table has no Show source", () => {
    renderTask({ ...FAILED, kind: "source_check" });

    expect(screen.queryByRole("button", { name: "Show source gold.sales" })).toBeNull();
    expect(screen.getByRole("link", { name: "Open table gold.sales" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Log gold.sales" })).toBeInTheDocument();
  });

  it("a skipped task says upstream failed from the servers cause", () => {
    renderTask(
      aTask({
        asset: "gold.ranking",
        status: "upstream_failed",
        started_at: null,
        finished_at: null,
        rows_written: null,
        failure: {
          code: "upstream_failed",
          cause: "gold.sales did not succeed.",
          next_step: "Fix gold.sales, then resume the run.",
        },
      }),
    );

    expect(secondLine()).toHaveTextContent(
      "upstream_failed (opens in a new tab) · Cause: gold.sales did not succeed. Next step: Fix gold.sales, then resume the run.",
    );
    expect(within(secondLine()).queryByRole("link", { name: "Log gold.sales" })).toBeNull();
  });

  it("a pending task shows why it waits", () => {
    renderTask(
      aTask({
        status: "pending",
        started_at: null,
        finished_at: null,
        rows_written: null,
        waiting_for: { code: "upstream", message: "It waits for silver.‮orders." },
      }),
    );

    expect(screen.getAllByRole("row")).toHaveLength(1);
    expect(screen.getByRole("row")).toHaveTextContent("It waits for silver.U+202Eorders.");
  });

  it("a succeeded task has one line", () => {
    renderTask(aTask());

    expect(screen.getAllByRole("row")).toHaveLength(1);
  });

  it("a failed check offers Show rows of that check", () => {
    renderTask({
      ...FAILED,
      failure: aFailure("check_blocked"),
      checks: [
        { name: "gold.sales/no_nulls", table: "gold.sales", outcome: "failed", failing_rows: 3 },
        { name: "gold.sales/unique", table: "gold.sales", outcome: "passed", failing_rows: 0 },
      ],
    });

    expect(screen.getAllByRole("button", { name: /Show rows of/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Show rows of gold.sales/no_nulls" })).toBeVisible();
  });

  it("offers no Open table for an asset that is not a dotted table", () => {
    renderTask({ ...FAILED, asset: "orders" });

    expect(screen.queryByRole("link", { name: /^Open table/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Show source/ })).toBeNull();
    expect(screen.getByRole("link", { name: "Log orders" })).toBeInTheDocument();
  });
});
