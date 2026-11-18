import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18next from "i18next";
import { afterEach, describe, expect, it, vi } from "vitest";

import { aDeploy, aJob, DEPLOY_ID } from "../test/jobs";
import { expectPending, held } from "../test/pending";
import { type FakeRoute, fakeServer } from "../test/server";
import type { RollbackTarget } from "./rollbackable";
import { RollbackDialog } from "./RollbackDialog";

const ENV = "/api/v1/environments/prod";
const JOBS = `GET ${ENV}/jobs`;
const DEPLOY = `GET ${ENV}/deploys/${DEPLOY_ID}`;
const ROLL = `POST ${ENV}/deploys`;

const when = (iso: string): string =>
  new Intl.DateTimeFormat(i18next.language, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );

/** The jobs `analytics` defines in `prod` now. */
const CURRENT_JOBS: FakeRoute = {
  status: 200,
  body: [aJob({ name: "nightly_sales" }), aJob({ name: "orders_hourly" })],
};

/** The target version with its jobs. */
const TARGET: FakeRoute = {
  status: 200,
  body: aDeploy({ jobs: ["nightly_sales", "backfill_2025"] }),
};

/**
 * Renders the dialog for `aDeploy()` over a server.
 *
 * @param routes - What the server answers.
 * @param protectedEnvironment - Whether `prod` is protected.
 * @returns The server, the handlers and a user-event session.
 */
function renderDialog(routes: Record<string, FakeRoute>, protectedEnvironment = false) {
  const server = fakeServer(routes);
  const onAccepted = vi.fn();
  const onCancel = vi.fn();
  render(
    <RollbackDialog
      environment="prod"
      deploy={aDeploy() as RollbackTarget}
      protectedEnvironment={protectedEnvironment}
      onAccepted={onAccepted}
      onCancel={onCancel}
    />,
  );
  return { server, onAccepted, onCancel, user: userEvent.setup() };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("RollbackDialog", () => {
  it("names the repository, environment, commit, when and who", async () => {
    renderDialog({ [JOBS]: CURRENT_JOBS, [DEPLOY]: TARGET });

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Roll back analytics in prod?");
    expect(dialog).toHaveTextContent(
      `This makes 4f9d2a7, deployed ${when(aDeploy().created_at)} by ana.nowak, the current version of analytics in prod again. Every job analytics defines there changes with it:`,
    );
    expect(await within(dialog).findByRole("button", { name: "Roll back" })).toBeInTheDocument();
  });

  it("names an account since deleted for a submitter that is gone", () => {
    fakeServer({ [JOBS]: CURRENT_JOBS, [DEPLOY]: TARGET });
    render(
      <RollbackDialog
        environment="prod"
        deploy={aDeploy({ submitted_by: null }) as RollbackTarget}
        protectedEnvironment={false}
        onAccepted={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("dialog")).toHaveTextContent("by an account since deleted, the");
  });

  it("lists every job the rollback changes, kept, removed and back", async () => {
    const { server } = renderDialog({ [JOBS]: CURRENT_JOBS, [DEPLOY]: TARGET });

    const jobs = await screen.findByRole("list", { name: "Jobs the rollback changes" });
    expect(
      within(jobs)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      "backfill_2025: back with the rollback",
      "nightly_sales",
      "orders_hourly: removed by the rollback",
    ]);
    expect(server.sent.map((request) => request.path)).toEqual([
      `${ENV}/jobs?repository=analytics`,
      `${ENV}/deploys/${DEPLOY_ID}`,
    ]);
  });

  it("says when neither version defines a job", async () => {
    renderDialog({
      [JOBS]: { status: 200, body: [] },
      [DEPLOY]: { status: 200, body: aDeploy({ jobs: [] }) },
    });

    expect(
      await screen.findByText("analytics defines no job in either version."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Roll back" })).toBeInTheDocument();
  });

  it("says it reads the jobs after a second", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const jobs = held();
    renderDialog({ [JOBS]: jobs.route, [DEPLOY]: TARGET });

    const status = screen.getByRole("status");
    expect(status).toBeEmptyDOMElement();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(status).toHaveTextContent("Reading the jobs this changes…");
    expect(screen.queryByRole("button", { name: "Roll back" })).toBeNull();
  });

  it("offers no Roll back until both reads answered", async () => {
    let fail = true;
    const { user } = renderDialog({
      [JOBS]: CURRENT_JOBS,
      [DEPLOY]: () =>
        fail
          ? { status: 500, body: { type: "InternalError", detail: "The server failed." } }
          : { status: 200, body: aDeploy({ jobs: ["nightly_sales"] }) },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/./u);
    expect(screen.queryByRole("button", { name: "Roll back" })).toBeNull();
    expect(screen.queryByRole("list")).toBeNull();

    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Roll back" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Jobs the rollback changes" })).toHaveTextContent(
      "orders_hourly: removed by the rollback",
    );
  });

  it("offers no Roll back when the server does not say which jobs the version defines", async () => {
    const { onCancel, user } = renderDialog({
      [JOBS]: CURRENT_JOBS,
      [DEPLOY]: { status: 200, body: aDeploy({ jobs: null }) },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The server did not say which jobs that version defines",
    );
    expect(screen.queryByRole("button", { name: "Roll back" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("marks a protected environment", async () => {
    renderDialog({ [JOBS]: CURRENT_JOBS, [DEPLOY]: TARGET }, true);

    expect(await screen.findByText("prod is protected.")).toBeInTheDocument();
    // Said once: the danger's dot beside the sentence, never the status and the sentence both.
    expect(screen.getByRole("dialog")).toHaveTextContent("● prod is protected.");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("· protected");
  });

  it("sends the rollback once with the commit and hands back the deploy's id", async () => {
    const answer = held();
    const { server, onAccepted, user } = renderDialog({
      [JOBS]: CURRENT_JOBS,
      [DEPLOY]: TARGET,
      [ROLL]: answer.route,
    });

    const confirm = await screen.findByRole("button", { name: "Roll back" });
    await user.click(confirm);
    await user.click(confirm);
    expectPending(confirm);
    answer.release({ status: 202, body: { id: "d-rollback", status: "queued" } });

    await waitFor(() => {
      expect(onAccepted).toHaveBeenCalledWith("d-rollback");
    });
    const sent = server.sent.filter((request) => request.method === "POST");
    expect(sent).toHaveLength(1);
    expect(sent[0]?.body).toEqual({
      repository: "analytics",
      kind: "rollback",
      to_sha: aDeploy().resolved_sha,
    });
  });

  it("keeps the dialog with the servers sentence on a refusal", async () => {
    const { onAccepted, user } = renderDialog({
      [JOBS]: CURRENT_JOBS,
      [DEPLOY]: TARGET,
      [ROLL]: {
        status: 403,
        body: { type: "PermissionDenied", detail: "You may not deploy to prod." },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Roll back" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("You may not deploy to prod.");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onAccepted).not.toHaveBeenCalled();
  });

  it("ignores Escape and Cancel while pending", async () => {
    const answer = held();
    const { onCancel, user } = renderDialog({
      [JOBS]: CURRENT_JOBS,
      [DEPLOY]: TARGET,
      [ROLL]: answer.route,
    });

    await user.click(await screen.findByRole("button", { name: "Roll back" }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).not.toHaveBeenCalled();

    answer.release({ status: 403, body: { type: "PermissionDenied", detail: "No." } });
    await screen.findByRole("alert");
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
