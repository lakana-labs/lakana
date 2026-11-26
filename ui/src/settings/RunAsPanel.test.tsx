import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { EnvironmentOut } from "../api/environments";
import { anEnvironment } from "../test/access";
import { aDeployment } from "../test/jobs";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami, withoutSessionReads } from "../test/server";

const PROD: EnvironmentOut = { ...anEnvironment("prod"), is_protected: true };
const RUN_AS = "/api/v1/environments/prod/deployments/analytics/run-as";

/**
 * prod's page, with `analytics` deployed and bound to nobody.
 *
 * @param routes - Routes that differ.
 * @param environment - What the server answers for prod.
 * @returns The shell.
 */
function prodPage(
  routes: Record<string, FakeRoute> = {},
  environment: EnvironmentOut = PROD,
): ReturnType<typeof renderShell> {
  return renderShell("/settings/environments/prod", {
    routes: {
      "GET /api/v1/environments/prod": { status: 200, body: environment },
      "GET /api/v1/environments/prod/usage": { status: 404, body: { detail: "none" } },
      "GET /api/v1/environments/prod/deployments": { status: 200, body: [aDeployment()] },
      "GET /api/v1/principals": { status: 200, body: [] },
      ...routes,
    },
  });
}

describe("RunAsPanel", () => {
  it("lists each deployment with its commit and who its runs write as", async () => {
    prodPage({
      "GET /api/v1/environments/prod/deployments": {
        status: 200,
        body: [aDeployment(), aDeployment({ repository: "dbt", run_as: "etl-prod" })],
      },
    });

    const region = await screen.findByRole("region", { name: "Deployments in prod" });
    const analytics = within(region).getByText("analytics").closest("tr") as HTMLElement;
    expect(analytics).toHaveTextContent("9c1e5b3");
    expect(analytics).toHaveTextContent("No account: runs are refused until one is bound");
    const dbt = within(region).getByText("dbt").closest("tr") as HTMLElement;
    expect(dbt).toHaveTextContent("etl-prod");
    expect(
      within(dbt).getByRole("button", { name: "Change the run-as account of dbt" }),
    ).toBeInTheDocument();
    expect(
      within(dbt).getByRole("button", { name: "Clear the run-as account of dbt" }),
    ).toBeInTheDocument();
    expect(
      within(analytics).queryByRole("button", { name: /Clear the run-as account/ }),
    ).toBeNull();
  });

  it("says what an unbound deployment does in an unprotected environment", async () => {
    prodPage({}, { ...PROD, is_protected: false });

    expect(
      await screen.findByText(
        "No account: runs started by a person write as that person; schedules do not run",
      ),
    ).toBeInTheDocument();
  });

  it("says when nothing is deployed, and when the deployments cannot be read", async () => {
    prodPage({ "GET /api/v1/environments/prod/deployments": { status: 200, body: [] } });
    expect(await screen.findByText("Nothing is deployed to prod yet.")).toBeInTheDocument();
  });

  it("binds a service account after the touch, then says so", async () => {
    let bound: string | null = null;
    const { server, user } = prodPage({
      "GET /api/v1/environments/prod/deployments": () => ({
        status: 200,
        body: [aDeployment({ run_as: bound })],
      }),
      [`PUT ${RUN_AS}`]: (request) => {
        bound = (request.body as { principal: string }).principal;
        return { status: 200, body: aDeployment({ run_as: bound }) };
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Set the run-as account of analytics" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Set the run-as account of analytics in prod",
    });
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    await user.type(within(dialog).getByRole("combobox", { name: "Service account" }), "etl-prod");
    await user.keyboard("{Escape}");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("Runs of analytics in prod now write as etl-prod."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    const change = await screen.findByRole("button", {
      name: "Change the run-as account of analytics",
    });
    await waitFor(() => {
      expect(change).toHaveFocus();
    });
    const puts = withoutSessionReads(server).filter((request) => request.method === "PUT");
    expect(puts.map((request) => request.body)).toEqual([{ principal: "etl-prod" }]);
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(true);
  });

  it("keeps the server's sentence for a name that is no service account", async () => {
    const answer = held();
    const { user } = prodPage({ [`PUT ${RUN_AS}`]: answer.route });

    await user.click(
      await screen.findByRole("button", { name: "Set the run-as account of analytics" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("combobox", { name: "Service account" }), "maria");
    await user.keyboard("{Escape}");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    answer.release({
      status: 404,
      body: {
        type: "NotFoundError",
        detail: "Principal 'maria' not found or not a service account.",
      },
    });

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Principal 'maria' not found or not a service account.",
    );
    expect(within(dialog).getByRole("combobox", { name: "Service account" })).toHaveValue("maria");
  });

  it("clears the binding after asking, without a touch", async () => {
    let bound: string | null = "etl-prod";
    const { server, user } = prodPage({
      "GET /api/v1/environments/prod/deployments": () => ({
        status: 200,
        body: [aDeployment({ run_as: bound })],
      }),
      [`DELETE ${RUN_AS}`]: () => {
        bound = null;
        return { status: 200, body: aDeployment({ run_as: null }) };
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Clear the run-as account of analytics" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Clear the run-as account of analytics in prod?",
    });
    expect(dialog).toHaveTextContent("runs of analytics are refused");
    await user.click(within(dialog).getByRole("button", { name: "Clear" }));

    expect(
      await screen.findByText("Runs of analytics in prod write as no account now."),
    ).toBeVisible();
    const set = await screen.findByRole("button", { name: "Set the run-as account of analytics" });
    await waitFor(() => {
      expect(set).toHaveFocus();
    });
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
  });

  it("shows the bindings read-only to someone who does not manage the environment", async () => {
    renderShell("/settings/environments/prod", {
      me: { name: "kim", admin: false },
      routes: {
        "GET /api/v1/whoami": {
          status: 200,
          body: whoami("kim", false, true, false, [], false, { readsEnvironments: true }),
        },
        "GET /api/v1/environments/prod": { status: 200, body: PROD },
        "GET /api/v1/environments/prod/deployments": {
          status: 200,
          body: [aDeployment({ run_as: "etl-prod" })],
        },
      },
    });

    const region = await screen.findByRole("region", { name: "Deployments in prod" });
    expect(within(region).getByText("etl-prod")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /run-as account/ })).toBeNull();
  });
});
