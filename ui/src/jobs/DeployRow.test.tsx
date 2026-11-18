import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { describe, expect, it, vi } from "vitest";

import type { DeployOut } from "../api/jobs";
import { aDeploy } from "../test/jobs";
import { DeployRow } from "./DeployRow";

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/**
 * Renders one row in a table.
 *
 * @param deploy - The deploy.
 * @param current - Whether it is current.
 * @param onRollBack - The rollback's opener.
 * @returns The row.
 */
function renderRow(
  deploy: DeployOut,
  current = false,
  onRollBack: (() => void) | null = null,
): HTMLElement {
  render(
    <table>
      <tbody>
        <DeployRow deploy={deploy} current={current} onRollBack={onRollBack} />
      </tbody>
    </table>,
  );
  return screen.getByRole("row");
}

describe("DeployRow", () => {
  it("shows status, kind, short commit, repository, who and when", () => {
    const deploy = aDeploy();
    const row = renderRow(deploy);

    expect(within(row).getByText("Succeeded")).toBeInTheDocument();
    expect(row).toHaveTextContent("Deploy");
    expect(within(row).getByText("4f9d2a7").closest("code")).toHaveClass("jobs-commit");
    expect(row).toHaveTextContent("analytics");
    expect(row).toHaveTextContent("ana.nowak");
    expect(within(row).getByText(when(deploy.created_at))).toHaveAttribute(
      "dateTime",
      deploy.created_at,
    );
    expect(row).not.toHaveTextContent("current");
  });

  it("writes each kind in words, and a ref or no commit before one is known", () => {
    const dryRun = renderRow(aDeploy({ kind: "dry_run", resolved_sha: null, status: "running" }));
    expect(dryRun).toHaveTextContent("Dry run");
    expect(dryRun).toHaveTextContent("main");
    dryRun.remove();

    const rollback = renderRow(
      aDeploy({ kind: "rollback", status: "queued", resolved_sha: null, requested_ref: null }),
    );
    expect(rollback).toHaveTextContent("Rollback");
    expect(rollback).toHaveTextContent("not known yet");
    expect(rollback.querySelector("code")).toBeNull();
  });

  it("says current in words on the current version", () => {
    expect(renderRow(aDeploy(), true)).toHaveTextContent("4f9d2a7 · current");
  });

  it("says an account since deleted for a submitter that is gone", () => {
    expect(renderRow(aDeploy({ submitted_by: null }))).toHaveTextContent(
      "an account since deleted",
    );
  });

  it("shows the first line of a failed deploys message with hidden characters marked", () => {
    const failed = renderRow(
      aDeploy({
        status: "failed",
        error_message:
          "lakana.yaml: job daily‮_sales has no select\nTraceback (most recent call last)",
      }),
    );
    expect(within(failed).getByText("Failed")).toBeInTheDocument();
    expect(failed).toHaveTextContent("lakana.yaml: job dailyU+202E_sales has no select");
    expect(failed).not.toHaveTextContent("Traceback");
    failed.remove();

    // A failure without a message still says why the cell is there.
    for (const message of [null, "\n"]) {
      const silent = renderRow(aDeploy({ status: "failed", error_message: message }));
      expect(silent).toHaveTextContent("The deploy failed without saying why.");
      silent.remove();
    }
  });

  it("has no row link and no chevron", () => {
    const row = renderRow(aDeploy());

    expect(within(row).queryByRole("link")).toBeNull();
    expect(row).not.toHaveTextContent("›");
    expect(within(row).queryByRole("button")).toBeNull();
  });

  it("offers Roll back to this… named by the repository and the commit", async () => {
    const onRollBack = vi.fn();
    const row = renderRow(aDeploy(), false, onRollBack);

    const button = within(row).getByRole("button", { name: "Roll back analytics to 4f9d2a7" });
    expect(button).toHaveTextContent("Roll back to this…");
    await userEvent.click(button);
    expect(onRollBack).toHaveBeenCalledTimes(1);
  });
});
