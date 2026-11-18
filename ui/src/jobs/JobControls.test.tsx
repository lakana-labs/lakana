import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { JobOut } from "../api/jobs";
import { aJob, aJobRun } from "../test/jobs";
import { expectPending, held } from "../test/pending";
import { fakeServer } from "../test/server";
import { RunNowButton, ScheduleButton } from "./JobControls";

const JOB = "/api/v1/environments/prod/jobs/nightly_sales";

/**
 * Renders Run now for `nightly_sales`.
 *
 * @param job - The job.
 * @returns The handler and a user-event session.
 */
function renderRunNow(job: JobOut = aJob()) {
  const onStarted = vi.fn();
  const user = userEvent.setup();
  render(
    <MemoryRouter>
      <RunNowButton environment="prod" job={job} primary onStarted={onStarted} />
    </MemoryRouter>,
  );
  return { onStarted, user };
}

/**
 * Renders the schedule's button for a job.
 *
 * @param job - The job.
 * @returns The handler and a user-event session.
 */
function renderSchedule(job: JobOut) {
  const onChanged = vi.fn();
  const user = userEvent.setup();
  const view = render(<ScheduleButton environment="prod" job={job} onChanged={onChanged} />);
  return { onChanged, user, view };
}

describe("RunNowButton", () => {
  it("Run now starts the job and reports the run", async () => {
    const run = aJobRun({ id: "r2", status: "queued" });
    const server = fakeServer({
      [`POST ${JOB}/runs`]: { status: 201, body: { run, waiting: null } },
    });
    const { onStarted, user } = renderRunNow();
    const button = screen.getByRole("button", { name: "Run now" });
    expect(button).toHaveClass("button-primary");

    await user.click(button);

    await waitFor(() => {
      expect(onStarted).toHaveBeenCalledWith({ run, waiting: null });
    });
    expect(server.sent).toHaveLength(1);
    expect(server.sent[0]?.body).toEqual({ repository: "analytics" });
    expect(button).toHaveFocus();
  });

  it("shows the pending sign and Working… while the start is open", async () => {
    const answer = held();
    fakeServer({ [`POST ${JOB}/runs`]: answer.route });
    const { onStarted, user } = renderRunNow();
    const button = screen.getByRole("button", { name: "Run now" });

    await user.click(button);

    expectPending(button);
    expect(screen.getByRole("status")).toHaveTextContent("Working…");
    answer.release({ status: 201, body: { run: aJobRun(), waiting: null } });
    await waitFor(() => {
      expect(onStarted).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("Run now reports a waiting start with its sentence and link", async () => {
    const waiting = { waits_for_run_id: "r1", message: "A run of nightly_sales is running." };
    fakeServer({ [`POST ${JOB}/runs`]: { status: 202, body: { run: null, waiting } } });
    const { onStarted, user } = renderRunNow();

    await user.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() => {
      expect(onStarted).toHaveBeenCalledWith({ run: null, waiting });
    });
  });

  it("a refusal shows the servers sentence through problemText and keeps the button usable", async () => {
    const server = fakeServer({
      [`POST ${JOB}/runs`]: {
        status: 403,
        body: { type: "PermissionDenied", detail: "You may not run jobs in \u202Eprod." },
      },
    });
    const { onStarted, user } = renderRunNow();
    const button = screen.getByRole("button", { name: "Run now" });

    await user.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You may not run jobs in U+202Eprod.",
    );
    expect(onStarted).not.toHaveBeenCalled();
    expect(button).not.toHaveAttribute("aria-disabled");
    await user.click(button);
    await waitFor(() => {
      expect(server.sent).toHaveLength(2);
    });
  });

  it("is not offered without the right to run", () => {
    renderRunNow(aJob({ actions: [] }));

    expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
  });
});

describe("ScheduleButton", () => {
  it("Pause schedule asks first, names the job and that a running run finishes, then pauses once", async () => {
    const answer = held();
    const server = fakeServer({
      "POST /api/v1/environments/prod/jobs/nightly_\u202Esales/pause": answer.route,
    });
    const { onChanged, user } = renderSchedule(aJob({ name: "nightly_\u202Esales" }));

    await user.click(screen.getByRole("button", { name: "Pause schedule" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Pause the schedule of nightly_U+202Esales?");
    expect(dialog).toHaveTextContent("A run that is running finishes.");
    expect(server.sent).toEqual([]);
    const confirm = screen.getAllByRole("button", { name: "Pause schedule" }).at(-1);
    if (confirm === undefined) {
      throw new Error("no confirm button");
    }
    await user.click(confirm);
    await user.click(confirm);
    const paused = aJob({ paused: true, state: "paused" });
    answer.release({ status: 200, body: paused });

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledWith(paused);
    });
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/environments/prod/jobs/nightly_%E2%80%AEsales/pause",
    ]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes the question without pausing", async () => {
    const server = fakeServer();
    const { onChanged, user } = renderSchedule(aJob());

    await user.click(screen.getByRole("button", { name: "Pause schedule" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onChanged).not.toHaveBeenCalled();
    expect(server.sent).toEqual([]);
  });

  it("Resume schedule resumes at once", async () => {
    const resumed = aJob();
    const server = fakeServer({ [`POST ${JOB}/resume`]: { status: 200, body: resumed } });
    const { onChanged, user } = renderSchedule(aJob({ paused: true, state: "paused" }));

    await user.click(screen.getByRole("button", { name: "Resume schedule" }));

    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledWith(resumed);
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(server.sent[0]?.body).toEqual({ repository: "analytics" });
  });

  it("a refused resume shows the servers sentence", async () => {
    fakeServer({
      [`POST ${JOB}/resume`]: {
        status: 403,
        body: { type: "PermissionDenied", detail: "You may not resume this schedule." },
      },
    });
    const { user } = renderSchedule(aJob({ paused: true }));

    await user.click(screen.getByRole("button", { name: "Resume schedule" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("You may not resume this schedule.");
  });

  it("offers nothing the actions or the triggers do not allow", () => {
    const { view } = renderSchedule(aJob({ actions: ["run"] }));
    expect(screen.queryByRole("button")).toBeNull();

    view.rerender(
      <ScheduleButton
        environment="prod"
        job={aJob({ paused: true, actions: ["run", "pause"] })}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();

    view.rerender(
      <ScheduleButton
        environment="prod"
        job={aJob({ triggers: [{ kind: "on_demand" }] })}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});
