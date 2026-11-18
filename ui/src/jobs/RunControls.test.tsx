import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type JSX, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { RunOut } from "../api/jobs";
import { aJobRun, aTask, JOB_RUN_ID } from "../test/jobs";
import { held } from "../test/pending";
import { fakeServer } from "../test/server";
import { CancelRunButton, ResumeRunButton } from "./RunControls";

const RUN = `/api/v1/environments/prod/runs/${JOB_RUN_ID}`;

/**
 * Cancel for a run, showing the run it hands back.
 *
 * @param props - The run.
 * @param props.start - The run at first.
 * @param props.onChanged - Told the run after a cancel.
 * @returns The button.
 */
function CancelHarness({
  start,
  onChanged,
}: {
  readonly start: RunOut;
  readonly onChanged: (run: RunOut) => void;
}): JSX.Element {
  const [run, setRun] = useState(start);
  return (
    <CancelRunButton
      environment="prod"
      run={run}
      name={"nightly_\u202Esales"}
      onChanged={(changed) => {
        setRun(changed);
        onChanged(changed);
      }}
    />
  );
}

const running = aJobRun({ status: "running", actions: ["cancel"], finished_at: null });

describe("CancelRunButton", () => {
  it("Cancel asks first, says finished tasks stay done, then cancels once and says Cancelling…", async () => {
    const answer = held();
    const server = fakeServer({ [`POST ${RUN}/cancel`]: answer.route });
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<CancelHarness start={running} onChanged={onChanged} />);

    await user.click(screen.getByRole("button", { name: "Cancel run" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Cancel the run of nightly_U+202Esales?");
    expect(dialog).toHaveTextContent("Finished tasks stay done.");
    expect(server.sent).toEqual([]);
    const confirm = screen.getAllByRole("button", { name: "Cancel run" }).at(-1);
    if (confirm === undefined) {
      throw new Error("no confirm button");
    }
    await user.click(confirm);
    await user.click(confirm);
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const cancelling = { ...running, cancel_requested: true };
    answer.release({ status: 200, body: cancelling });

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledWith(cancelling);
    });
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      `POST ${RUN}/cancel`,
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("Cancelling…")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("Cancelling…").closest(".jobs-control")).toHaveFocus();
    });
    expect(screen.queryByRole("button", { name: "Cancel run" })).toBeNull();
  });

  it("Cancel of a run that already ended shows the servers 409 sentence", async () => {
    fakeServer({
      [`POST ${RUN}/cancel`]: {
        status: 409,
        body: { type: "ConflictError", detail: "The run has already ended." },
      },
    });
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<CancelHarness start={running} onChanged={onChanged} />);

    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    const confirm = screen.getAllByRole("button", { name: "Cancel run" }).at(-1);
    if (confirm === undefined) {
      throw new Error("no confirm button");
    }
    await user.click(confirm);

    expect(await screen.findByRole("alert")).toHaveTextContent("The run has already ended.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is not offered for an ended run or without the right to cancel", () => {
    const { rerender } = render(
      <CancelRunButton
        environment="prod"
        run={aJobRun({ actions: ["cancel"] })}
        name="nightly_sales"
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();

    rerender(
      <CancelRunButton
        environment="prod"
        run={{ ...running, actions: [] }}
        name="nightly_sales"
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});

const failed = aJobRun({
  status: "failed",
  actions: ["resume"],
  failed_tasks: 1,
  tasks: [aTask(), aTask({ asset: "gold.sales", status: "failed" })],
});

describe("ResumeRunButton", () => {
  it("Resume starts the failed part and hands back the new run", async () => {
    const next = aJobRun({ id: "r3", kind: "resume", status: "queued" });
    const server = fakeServer({ [`POST ${RUN}/resume`]: { status: 201, body: next } });
    const onResumed = vi.fn();
    const user = userEvent.setup();
    render(<ResumeRunButton environment="prod" run={failed} onResumed={onResumed} />);

    await user.click(screen.getByRole("button", { name: "Resume" }));

    await waitFor(() => {
      expect(onResumed).toHaveBeenCalledWith(next);
    });
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      `POST ${RUN}/resume`,
    ]);
  });

  it("a refused resume shows the servers sentence and stays usable", async () => {
    fakeServer({
      [`POST ${RUN}/resume`]: {
        status: 409,
        body: { type: "ConflictError", detail: "The project changed since this run." },
      },
    });
    const user = userEvent.setup();
    render(<ResumeRunButton environment="prod" run={failed} onResumed={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Resume" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The project changed since this run.",
    );
    expect(screen.getByRole("button", { name: "Resume" })).not.toHaveAttribute("aria-disabled");
  });

  it("Resume is not offered for a succeeded run or one with nothing to resume", () => {
    const { rerender } = render(
      <ResumeRunButton
        environment="prod"
        run={aJobRun({ actions: ["resume"] })}
        onResumed={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();

    rerender(
      <ResumeRunButton
        environment="prod"
        run={{ ...failed, tasks: [aTask()] }}
        onResumed={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();

    rerender(
      <ResumeRunButton environment="prod" run={{ ...failed, actions: [] }} onResumed={vi.fn()} />,
    );
    expect(screen.queryByRole("button")).toBeNull();

    rerender(
      <ResumeRunButton
        environment="prod"
        run={{
          ...failed,
          status: "cancelled",
          tasks: [aTask({ status: "cancelled" }), aTask({ status: "upstream_failed" })],
        }}
        onResumed={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  });
});
