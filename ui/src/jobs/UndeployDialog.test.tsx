import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { aJob } from "../test/jobs";
import { expectPending, held } from "../test/pending";
import { type FakeRoute, fakeServer } from "../test/server";
import { UndeployDialog } from "./UndeployDialog";

const ENV = "/api/v1/environments/prod";
const JOBS = `GET ${ENV}/jobs`;
const UNDEPLOY = `DELETE ${ENV}/deployments/analytics`;

/** The jobs `analytics` defines in `prod`: scheduled or on demand, with or without a waiting start. */
const THE_JOBS: FakeRoute = {
  status: 200,
  body: [
    aJob({ name: "nightly_sales" }),
    aJob({ name: "orders_backfill", cron: null, timezone: null, triggers: [] }),
    aJob({
      name: "stock_refresh",
      cron: null,
      timezone: null,
      triggers: [],
      state: "waiting",
      waiting_since: "2026-11-17T09:12:00Z",
      waiting_trigger: "manual",
    }),
    aJob({
      name: "weekly_report",
      state: "waiting",
      waiting_since: "2026-11-17T02:00:00Z",
      waiting_trigger: "schedule",
    }),
  ],
};

/**
 * Renders the dialog for `analytics` in `prod` over a server.
 *
 * @param routes - What the server answers.
 * @param options - The run principal bound and whether `prod` is protected.
 * @param options.runAs - The run principal.
 * @param options.protectedEnvironment - Whether `prod` is protected.
 * @returns The server, the handlers and a user-event session.
 */
function renderDialog(
  routes: Record<string, FakeRoute>,
  {
    runAs = "svc-analytics",
    protectedEnvironment = false,
  }: { runAs?: string | null; protectedEnvironment?: boolean } = {},
) {
  const server = fakeServer(routes);
  const onDone = vi.fn();
  const onCancel = vi.fn();
  render(
    <UndeployDialog
      environment="prod"
      repository="analytics"
      runAs={runAs}
      protectedEnvironment={protectedEnvironment}
      onDone={onDone}
      onCancel={onCancel}
    />,
  );
  return { server, onDone, onCancel, user: userEvent.setup() };
}

describe("UndeployDialog", () => {
  it("names every job that goes, with its schedule and a waiting start, and what else goes and stays", async () => {
    renderDialog({ [JOBS]: THE_JOBS });

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Undeploy analytics from prod?");
    const gone = await within(dialog).findByRole("list", { name: "What the undeploy removes" });
    expect(
      within(gone)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual([
      "nightly_sales: removed, with its schedule",
      "orders_backfill: removed",
      "stock_refresh: removed; its waiting start is dropped",
      "weekly_report: removed, with its schedule; its waiting start is dropped",
      "The service account its runs write as, svc-analytics: no longer bound",
      "Its glossary terms in prod",
      "The health effects of its tables: every health signal of these tables warns",
    ]);
    expect(dialog).toHaveTextContent(
      "Its tables and their data stay, as orphans, and its deploys stay listed. Deploying it again brings its jobs back.",
    );
  });

  it("asks which jobs go from the repository's own list, and says when it defines none", async () => {
    const { server } = renderDialog({ [JOBS]: { status: 200, body: [] } }, { runAs: null });

    expect(await screen.findByText("analytics defines no job in prod.")).toBeInTheDocument();
    expect(server.sent[0]?.path).toBe(`${ENV}/jobs?repository=analytics`);
    // No run principal is bound, so none is named.
    expect(screen.getByRole("dialog")).not.toHaveTextContent("service account");
  });

  it("names a protected environment so", async () => {
    renderDialog({ [JOBS]: THE_JOBS }, { protectedEnvironment: true });

    expect(await screen.findByText("prod is protected.")).toBeInTheDocument();
  });

  it("holds Undeploy, and says why, until the repository's name is typed exactly", async () => {
    const { server, onDone, user } = renderDialog({
      [JOBS]: THE_JOBS,
      [UNDEPLOY]: { status: 204 },
    });
    const button = await screen.findByRole("button", { name: "Undeploy" });
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(
      "Type the repository's name exactly to undeploy it.",
    );

    await user.type(screen.getByRole("textbox", { name: "Type analytics to confirm" }), "analytic");
    expect(button).toBeDisabled();
    await user.type(screen.getByRole("textbox"), "s");
    expect(button).toBeEnabled();
    expect(button).not.toHaveAccessibleDescription();

    await user.click(button);
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledOnce();
    });
    expect(server.sent.map((each) => [each.method, each.path])).toContainEqual([
      "DELETE",
      `${ENV}/deployments/analytics`,
    ]);
  });

  it("undeploys on Enter in the field once the name is typed", async () => {
    const { onDone, user } = renderDialog({ [JOBS]: THE_JOBS, [UNDEPLOY]: { status: 204 } });
    const field = await screen.findByRole("textbox");

    await user.type(field, "analytics{Enter}");

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledOnce();
    });
  });

  it("shows the server's refusal in its words, takes the focus to it and stays open", async () => {
    const sentence =
      "Run 1a2b3c4d of 'analytics' is active in 'prod'; cancel it or wait for it to finish before undeploying.";
    const { onDone, user } = renderDialog({
      [JOBS]: THE_JOBS,
      [UNDEPLOY]: { status: 409, body: { type: "ConflictError", detail: sentence } },
    });
    await user.type(await screen.findByRole("textbox"), "analytics");

    await user.click(screen.getByRole("button", { name: "Undeploy" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(sentence);
    await waitFor(() => {
      expect(alert).toHaveFocus();
    });
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("while the undeploy is sent, shows that it waits and Escape and Cancel do nothing", async () => {
    const answer = held();
    const { server, onCancel, onDone, user } = renderDialog({
      [JOBS]: THE_JOBS,
      [UNDEPLOY]: () => answer.route(),
    });
    await user.type(await screen.findByRole("textbox"), "analytics");
    const button = screen.getByRole("button", { name: "Undeploy" });

    await user.click(button);

    expectPending(button);
    expect(screen.getByText("Working…", { selector: ".step-status" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).not.toHaveBeenCalled();
    // Enter in the field sends no second undeploy.
    await user.type(screen.getByRole("textbox"), "{Enter}");
    expect(server.sent.filter((each) => each.method === "DELETE")).toHaveLength(1);
    answer.release({ status: 204 });
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledOnce();
    });
  });

  it("offers no Undeploy while the jobs cannot be read, so it never removes one it did not name", async () => {
    const { server, onCancel, user } = renderDialog({
      [JOBS]: { status: 503, body: { type: "Unavailable", detail: "The server is busy." } },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy.");
    expect(screen.queryByRole("button", { name: "Undeploy" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();

    server.route({ [JOBS]: THE_JOBS });
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Undeploy" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("says that it reads the jobs after a second", async () => {
    const jobs = held();
    renderDialog({ [JOBS]: () => jobs.route() });

    expect(
      await screen.findByText("Reading the jobs this removes…", {}, { timeout: 2_000 }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undeploy" })).toBeNull();
    jobs.release(THE_JOBS);
    expect(await screen.findByRole("button", { name: "Undeploy" })).toBeInTheDocument();
  });
});
