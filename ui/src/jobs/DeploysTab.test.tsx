import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import type { DeployOut } from "../api/jobs";
import { aDeploy, aDeployment, DEPLOY_VERSION, ENVIRONMENTS } from "../test/jobs";
import { held } from "../test/pending";
import { type FakeAnswer, type FakeRoute, fakeServer } from "../test/server";
import { DeploysTab } from "./DeploysTab";

const CURRENT = aDeployment();

/**
 * Shows the address the tab is at.
 *
 * @returns The address's query.
 */
function Address() {
  const location = useLocation();
  return <output data-testid="address">{location.search}</output>;
}

/**
 * Renders the tab at an address over a server whose `prod` and `dev` answer the same deploys.
 *
 * @param deploys - What `GET …/deploys` answers.
 * @param path - The address.
 * @param deployments - What `GET …/deployments` answers.
 * @returns The server and a user-event session.
 */
function renderTab(
  deploys: FakeRoute,
  path = "/jobs/deploys",
  deployments: FakeRoute = { status: 200, body: [CURRENT] },
) {
  const server = fakeServer({
    "GET /api/v1/environments": { status: 200, body: ENVIRONMENTS },
    "GET /api/v1/environments/dev/deploys": deploys,
    "GET /api/v1/environments/prod/deploys": deploys,
    "GET /api/v1/environments/dev/deployments": deployments,
    "GET /api/v1/environments/prod/deployments": deployments,
  });
  render(
    <MemoryRouter initialEntries={[path]}>
      <DeploysTab />
      <Address />
    </MemoryRouter>,
  );
  return { server, user: userEvent.setup() };
}

const list = (items: readonly DeployOut[], next?: string): FakeAnswer => ({
  status: 200,
  body: items,
  ...(next === undefined ? {} : { headers: { "X-Next-Cursor": next } }),
});

/** The current deploy of `analytics`. */
const NOW = aDeploy({
  id: "d-now",
  project_version_id: CURRENT.project_version_id,
  resolved_sha: CURRENT.commit_sha,
  created_at: "2026-11-17T08:00:00Z",
});

afterEach(() => {
  window.localStorage.clear();
});

describe("DeploysTab", () => {
  it("lists one environment's deploys, the shared filter's or else the first, and says so", async () => {
    const { server } = renderTab(list([NOW, aDeploy()]));

    const region = await screen.findByRole("region", { name: "Deploys of dev" });
    expect(screen.getByText("Deploys are listed one environment at a time.")).toBeInTheDocument();
    expect(within(region).getAllByRole("row")).toHaveLength(3);
    expect(server.sent.map((request) => request.path)).toContain(
      "/api/v1/environments/dev/deploys?limit=50",
    );
    // No "All environments": the picker names the one listed.
    expect(screen.getByRole("button", { name: /^Environment/u })).toHaveTextContent("dev");
    await waitFor(() => {
      expect(within(region).getAllByRole("row")[1]).toHaveTextContent("current");
    });
  });

  it("counts the deploys and reads them and the current versions again on Refresh", async () => {
    const { server, user } = renderTab(list([NOW, aDeploy()]));
    await screen.findByRole("region", { name: "Deploys of dev" });
    expect(await screen.findByText(/^(\d+ deploys?|The first \d+ deploys?)$/)).toHaveClass(
      "toolbar-count",
    );
    const reads = (): number =>
      server.sent.filter((request) => request.path.includes("/deploys")).length;
    const before = reads();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(reads()).toBeGreaterThan(before);
    });
  });

  it("starts from the shared filter's environment and pages with Show more", async () => {
    const { server, user } = renderTab(
      (request) =>
        request.path.includes("cursor=d2") ? list([aDeploy({ id: "d-old" })]) : list([NOW], "d2"),
      "/jobs/deploys?environment=prod",
    );

    await screen.findByRole("region", { name: "Deploys of prod" });
    await user.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(3);
    });
    expect(server.sent.at(-1)?.path).toBe("/api/v1/environments/prod/deploys?limit=50&cursor=d2");
  });

  it("filters by repository through the address", async () => {
    const { server, user } = renderTab(list([NOW]));
    await screen.findByRole("region", { name: "Deploys of dev" });

    await user.click(screen.getByRole("button", { name: /Repository/u }));
    await user.click(await screen.findByRole("option", { name: "analytics" }));
    await waitFor(() => {
      expect(server.sent.at(-1)?.path).toBe(
        "/api/v1/environments/dev/deploys?repository=analytics&limit=50",
      );
    });
    expect(screen.getByTestId("address")).toHaveTextContent("?repository=analytics");

    await user.click(screen.getByRole("button", { name: /Repository/u }));
    await user.click(screen.getByRole("option", { name: "All repositories" }));
    expect(screen.getByTestId("address")).toBeEmptyDOMElement();
  });

  it("offers a repository from the address that is not deployed, and says none was deployed", async () => {
    renderTab(list([]), "/jobs/deploys?repository=team%2Fbilling");

    expect(await screen.findByText("No deploy of team/billing in dev yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Repository/u })).toHaveTextContent("team/billing");
  });

  it("says when an environment has no deploy", async () => {
    renderTab(list([]));

    expect(await screen.findByText("No deploy in dev yet.")).toBeInTheDocument();
  });

  it("offers Roll back only beside earlier successful deploys and rollbacks the person may roll back", async () => {
    renderTab(
      list([
        NOW,
        aDeploy({ id: "d-dry", kind: "dry_run", project_version_id: null }),
        aDeploy({ id: "d-failed", status: "failed", project_version_id: null }),
        aDeploy({ id: "d-queued", status: "queued", resolved_sha: null, project_version_id: null }),
        aDeploy({ id: "d-older" }),
        aDeploy({
          id: "d-rollback",
          kind: "rollback",
          requested_ref: null,
          resolved_sha: "0be1f6a9d2c3b4a5968778695a4b3c2d1e0f9a8b",
          project_version_id: "2c3d4e5f-6071-4829-8a3b-4c5d6e7f8091",
        }),
        aDeploy({ id: "d-reader", actions: [] }),
      ]),
    );
    const region = await screen.findByRole("region", { name: "Deploys of dev" });
    await waitFor(() => {
      expect(within(region).getAllByRole("button")).toHaveLength(2);
    });

    const buttons = within(region).getAllByRole("button");
    expect(buttons[0]).toHaveAccessibleName("Roll back to this… (analytics at 4f9d2a7)");
    expect(buttons[1]).toHaveAccessibleName("Roll back to this… (analytics at 0be1f6a)");
  });

  it("marks only the newest run of the current version current after a rollback to it", async () => {
    const back = aDeploy({
      id: "d-back",
      kind: "rollback",
      requested_ref: null,
      project_version_id: CURRENT.project_version_id,
      resolved_sha: CURRENT.commit_sha,
      created_at: "2026-11-18T08:00:00Z",
    });
    renderTab(list([back, aDeploy(), NOW]));

    const region = await screen.findByRole("region", { name: "Deploys of dev" });
    const rows = within(region).getAllByRole("row");
    await waitFor(() => {
      expect(rows[1]).toHaveTextContent("current");
    });
    expect(rows[3]).not.toHaveTextContent("current");
  });

  it("marks nothing current and offers no rollback while the current versions cannot be read", async () => {
    const { server, user } = renderTab(list([NOW, aDeploy()]), "/jobs/deploys", {
      status: 503,
      body: { type: "Unavailable", detail: "The server is busy." },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The current versions could not be read, so no deploy is marked current and none offers a rollback:",
    );
    const region = await screen.findByRole("region", { name: "Deploys of dev" });
    expect(within(region).queryByRole("button")).toBeNull();
    expect(region).not.toHaveTextContent("current");

    server.route({
      "GET /api/v1/environments/dev/deployments": { status: 200, body: [CURRENT] },
    });
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await within(region).findByRole("button", {
        name: "Roll back to this… (analytics at 4f9d2a7)",
      }),
    ).toBeInTheDocument();
  });

  it("after a rollback the focus returns to the rows button and the list is read again", async () => {
    let rolledBack = false;
    let reads = 0;
    const read = held();
    const rollbackRow = aDeploy({
      id: "d-rollback",
      kind: "rollback",
      requested_ref: null,
      project_version_id: aDeploy().project_version_id,
      created_at: "2026-11-18T08:00:00Z",
    });
    const { server, user } = renderTab(
      () => list(rolledBack ? [rollbackRow, NOW, aDeploy()] : [NOW, aDeploy()]),
      "/jobs/deploys?environment=prod",
      () => ({
        status: 200,
        body: [rolledBack ? aDeployment({ project_version_id: DEPLOY_VERSION }) : CURRENT],
      }),
    );
    server.route({
      "GET /api/v1/environments/prod/jobs": { status: 200, body: [] },
      "GET /api/v1/environments/prod/deploys/d-rollback": async () => {
        reads += 1;
        const answer = await read.route();
        rolledBack = true;
        return answer;
      },
      [`GET /api/v1/environments/prod/deploys/${aDeploy().id}`]: {
        status: 200,
        body: aDeploy({ jobs: [] }),
      },
      "POST /api/v1/environments/prod/deploys": {
        status: 202,
        body: { id: "d-rollback", status: "queued" },
      },
    });

    const button = await screen.findByRole("button", {
      name: "Roll back to this… (analytics at 4f9d2a7)",
    });
    await user.click(button);
    const dialog = await screen.findByRole("dialog", { name: "Roll back analytics in prod?" });
    expect(dialog).toHaveTextContent("prod is protected.");
    await user.click(await within(dialog).findByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(button).toHaveFocus();
    });

    await user.click(button);
    await user.click(await screen.findByRole("button", { name: "Roll back" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await waitFor(() => {
      expect(button).toHaveFocus();
    });
    expect(screen.getByText("Rolling back analytics in prod to 4f9d2a7…")).toBeInTheDocument();
    // While it runs, the row's button stays but starts no second rollback, and says why.
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAccessibleDescription("Rolling back analytics in prod to 4f9d2a7…");
    await user.click(button);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button).toHaveFocus();
    read.release({ status: 200, body: rollbackRow });

    // The note's read finds it succeeded: the list and the current versions are read again, and
    // the row's button leaves with the focus, which goes to the note.
    expect(
      await screen.findByText("Rolled back analytics in prod to 4f9d2a7."),
    ).toBeInTheDocument();
    expect(reads).toBe(1);
    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(4);
    });
    expect(
      screen.queryByRole("button", { name: "Roll back to this… (analytics at 4f9d2a7)" }),
    ).toBeNull();
    await waitFor(() => {
      expect(
        screen.getByText("Rolled back analytics in prod to 4f9d2a7.").closest("[tabindex]"),
      ).toHaveFocus();
    });
  });

  it("holds every Roll back to this… while a rollback is followed, of any repository", async () => {
    const read = held();
    const billingOld = aDeploy({
      id: "d-billing",
      repository: "billing",
      resolved_sha: "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f",
      project_version_id: "4d5e6f70-8192-4a3b-9c4d-5e6f708192a3",
    });
    const { server, user } = renderTab(
      list([NOW, aDeploy(), billingOld]),
      "/jobs/deploys?environment=prod",
      {
        status: 200,
        body: [CURRENT, aDeployment({ repository: "billing" })],
      },
    );
    server.route({
      "GET /api/v1/environments/prod/jobs": { status: 200, body: [] },
      "GET /api/v1/environments/prod/deploys/d-rollback": () => read.route(),
      [`GET /api/v1/environments/prod/deploys/${aDeploy().id}`]: {
        status: 200,
        body: aDeploy({ jobs: [] }),
      },
      "POST /api/v1/environments/prod/deploys": {
        status: 202,
        body: { id: "d-rollback", status: "queued" },
      },
    });
    const analytics = await screen.findByRole("button", {
      name: "Roll back to this… (analytics at 4f9d2a7)",
    });
    const billing = screen.getByRole("button", { name: "Roll back to this… (billing at 5e6f7a8)" });
    await user.click(analytics);
    await user.click(await screen.findByRole("button", { name: "Roll back" }));
    await screen.findByText("Rolling back analytics in prod to 4f9d2a7…");

    // Another repository's rollback would replace the note that follows this one.
    expect(billing).toHaveAttribute("aria-disabled", "true");
    expect(billing).toHaveAccessibleDescription("Rolling back analytics in prod to 4f9d2a7…");
    await user.click(billing);
    expect(screen.queryByRole("dialog")).toBeNull();

    read.release({ status: 200, body: aDeploy({ id: "d-rollback", kind: "rollback" }) });
    await screen.findByText("Rolled back analytics in prod to 4f9d2a7.");
    await waitFor(() => {
      expect(billing).not.toHaveAttribute("aria-disabled");
    });
  });

  it("hands the focus to the note when the list answers before the current versions", async () => {
    let rolledBack = false;
    const versions = held();
    const rollbackRow = aDeploy({
      id: "d-rollback",
      kind: "rollback",
      requested_ref: null,
      created_at: "2026-11-18T08:00:00Z",
    });
    const { server, user } = renderTab(
      () => list(rolledBack ? [rollbackRow, NOW, aDeploy()] : [NOW, aDeploy()]),
      "/jobs/deploys?environment=prod",
      () => (rolledBack ? versions.route() : { status: 200, body: [CURRENT] }),
    );
    server.route({
      "GET /api/v1/environments/prod/jobs": { status: 200, body: [] },
      "GET /api/v1/environments/prod/deploys/d-rollback": () => {
        rolledBack = true;
        return { status: 200, body: rollbackRow };
      },
      [`GET /api/v1/environments/prod/deploys/${aDeploy().id}`]: {
        status: 200,
        body: aDeploy({ jobs: [] }),
      },
      "POST /api/v1/environments/prod/deploys": {
        status: 202,
        body: { id: "d-rollback", status: "queued" },
      },
    });

    const button = await screen.findByRole("button", {
      name: "Roll back to this… (analytics at 4f9d2a7)",
    });
    await user.click(button);
    await user.click(await screen.findByRole("button", { name: "Roll back" }));
    expect(
      await screen.findByText("Rolled back analytics in prod to 4f9d2a7."),
    ).toBeInTheDocument();
    // The list is read again first; the button is still there, as the old versions are.
    await waitFor(() => {
      expect(screen.getAllByRole("row")).toHaveLength(4);
    });
    await waitFor(() => {
      expect(button).toHaveFocus();
    });

    // Then the current versions answer, and the button leaves with the focus.
    versions.release({
      status: 200,
      body: [aDeployment({ project_version_id: DEPLOY_VERSION })],
    });
    await waitFor(() => {
      expect(button).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(
        screen.getByText("Rolled back analytics in prod to 4f9d2a7.").closest("[tabindex]"),
      ).toHaveFocus();
    });
  });

  it("a choice of environment writes the shared filter", async () => {
    const { server, user } = renderTab(list([NOW]));
    await screen.findByRole("region", { name: "Deploys of dev" });

    await user.click(screen.getByRole("button", { name: /^Environment/u }));
    await user.click(screen.getByRole("option", { name: /^prod/u }));

    expect(await screen.findByRole("region", { name: "Deploys of prod" })).toBeInTheDocument();
    expect(screen.getByTestId("address")).toHaveTextContent("?environment=prod");
    expect(window.localStorage.getItem("lakana.jobs.environment")).toBe("prod");
    expect(server.sent.map((request) => request.path)).toContain(
      "/api/v1/environments/prod/deployments",
    );
  });
});
