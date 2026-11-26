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

  it("offers every repository with deploys listed, not only those deployed now, and keeps them", async () => {
    const billing = aDeploy({ id: "d-billing", repository: "billing" });
    const { user } = renderTab((request) =>
      list(request.path.includes("repository=billing") ? [billing] : [NOW, billing]),
    );
    await screen.findByRole("region", { name: "Deploys of dev" });
    const offered = async (): Promise<string[]> => {
      await user.click(screen.getByRole("button", { name: /Repository/u }));
      const names = (await screen.findAllByRole("option")).map((option) => option.textContent);
      await user.keyboard("{Escape}");
      return names;
    };

    // billing is no longer deployed in dev, but its deploys are in the list.
    expect(await offered()).toEqual(["All repositories", "analytics", "billing"]);
    // The menu says where its choices come from.
    await user.click(screen.getByRole("button", { name: /Repository/u }));
    const note =
      "Lists the repositories deployed in dev now and those whose deploys this list has shown.";
    expect(screen.getByText(note)).toBeVisible();
    expect(screen.getByRole("listbox")).toHaveAccessibleDescription(note);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: /Repository/u }));
    await user.click(await screen.findByRole("option", { name: "billing" }));
    await waitFor(() => {
      expect(screen.getByTestId("address")).toHaveTextContent("?repository=billing");
    });
    // The list now holds billing's deploys only; the menu still offers the others seen.
    expect(await offered()).toEqual(["All repositories", "analytics", "billing"]);
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
      expect(within(region).getAllByRole("button", { name: /^Roll back/u })).toHaveLength(2);
    });

    const buttons = within(region).getAllByRole("button", { name: /^Roll back/u });
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
    expect(within(region).queryByRole("button", { name: /^Roll back/u })).toBeNull();
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

  it.each([
    {
      end: "succeeded",
      answer: aDeploy({ id: "d-rollback", kind: "rollback" }),
      said: "Rolled back analytics in prod to 4f9d2a7.",
    },
    {
      end: "failed",
      answer: aDeploy({
        id: "d-rollback",
        kind: "rollback",
        status: "failed",
        error_message: "lakana.yaml at 4f9d2a7 is not valid",
      }),
      said: "The rollback failed: lakana.yaml at 4f9d2a7 is not valid",
    },
  ])(
    "holds every Roll back to this… while a rollback is followed, of any repository, until it $end",
    async ({ answer, said }) => {
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
      const billing = screen.getByRole("button", {
        name: "Roll back to this… (billing at 5e6f7a8)",
      });
      await user.click(analytics);
      await user.click(await screen.findByRole("button", { name: "Roll back" }));
      await screen.findByText("Rolling back analytics in prod to 4f9d2a7…");

      // Another repository's rollback would replace the note that follows this one, and an
      // undeploy would end what it follows.
      const undeploy = screen.getByRole("button", { name: "Undeploy… (analytics from prod)" });
      expect(undeploy).toHaveAttribute("aria-disabled", "true");
      expect(undeploy).toHaveAccessibleDescription("Rolling back analytics in prod to 4f9d2a7…");
      await user.click(undeploy);
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(billing).toHaveAttribute("aria-disabled", "true");
      expect(billing).toHaveAccessibleDescription("Rolling back analytics in prod to 4f9d2a7…");
      await user.click(billing);
      expect(screen.queryByRole("dialog")).toBeNull();

      read.release({ status: 200, body: answer });
      await screen.findByText(said);
      await waitFor(() => {
        expect(billing).not.toHaveAttribute("aria-disabled");
      });
      expect(analytics).not.toHaveAttribute("aria-disabled");
      expect(billing).not.toHaveAccessibleDescription();
    },
  );

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

  it("offers Undeploy… only beside the current deploy, to a person who may undeploy", async () => {
    renderTab(
      list([NOW, aDeploy(), aDeploy({ id: "d-failed", status: "failed" })]),
      "/jobs/deploys?environment=prod",
    );
    const region = await screen.findByRole("region", { name: "Deploys of prod" });

    const button = await within(region).findByRole("button", { name: /^Undeploy…/u });
    expect(button).toHaveAccessibleName("Undeploy… (analytics from prod)");
    expect(within(region).getAllByRole("button", { name: /^Undeploy…/u })).toHaveLength(1);
    expect(button.closest("tr")).toHaveTextContent("current");
  });

  it("offers no Undeploy… to a person who may not undeploy", async () => {
    renderTab(list([{ ...NOW, actions: ["rollback"] }, aDeploy()]));
    const region = await screen.findByRole("region", { name: "Deploys of dev" });
    await within(region).findByRole("button", { name: /^Roll back/u });
    expect(within(region).queryByRole("button", { name: /^Undeploy/u })).toBeNull();
  });

  it("after an undeploy the note says so and takes the focus, and the row buttons wait for the current versions", async () => {
    let undeployed = false;
    const versions = held();
    const billingNow = aDeploy({
      id: "d-billing-now",
      repository: "billing",
      project_version_id: "5e6f7081-92a3-4b4c-8d5e-6f708192a3b4",
      resolved_sha: "6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a",
    });
    const billingOld = aDeploy({
      id: "d-billing-old",
      repository: "billing",
      resolved_sha: "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f",
      project_version_id: "4d5e6f70-8192-4a3b-9c4d-5e6f708192a3",
      created_at: "2026-11-15T09:00:00Z",
    });
    const billing = aDeployment({
      repository: "billing",
      project_version_id: billingNow.project_version_id ?? "",
      commit_sha: billingNow.resolved_sha ?? "",
    });
    const { server, user } = renderTab(
      list([NOW, aDeploy(), billingNow, billingOld]),
      "/jobs/deploys?environment=prod",
      () =>
        undeployed
          ? versions.route()
          : { status: 200, body: [aDeployment({ run_as: "svc-analytics" }), billing] },
    );
    server.route({
      "GET /api/v1/environments/prod/jobs": { status: 200, body: [] },
      "DELETE /api/v1/environments/prod/deployments/analytics": () => {
        undeployed = true;
        return { status: 204 };
      },
    });

    const button = await screen.findByRole("button", {
      name: "Undeploy… (analytics from prod)",
    });
    await user.click(button);
    const dialog = await screen.findByRole("dialog", { name: "Undeploy analytics from prod?" });
    expect(dialog).toHaveTextContent("prod is protected.");
    expect(dialog).toHaveTextContent(
      "The service account its runs write as, svc-analytics: no longer bound",
    );
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(button).toHaveFocus();
    });

    await user.click(button);
    await user.type(await screen.findByRole("textbox"), "analytics");
    await user.click(screen.getByRole("button", { name: "Undeploy" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    // It names the version the row showed as current, so a deployment moved since is refused.
    const asked = server.sent.find((each) => each.method === "DELETE");
    expect(asked?.path).toBe(
      `/api/v1/environments/prod/deployments/analytics?expected_project_version_id=${aDeployment().project_version_id}`,
    );

    // The deployment is gone: nothing of analytics is offered, and the note has the focus.
    const said = "Undeployed analytics from prod. Its tables stay, as orphans.";
    expect(await screen.findByText(said)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(said).closest("[tabindex]")).toHaveFocus();
    });
    expect(screen.queryByRole("button", { name: /analytics/u })).toBeNull();
    expect(screen.getByRole("region", { name: "Deploys of prod" })).not.toHaveTextContent(
      "4f9d2a7 · current",
    );
    // Until the current versions are read again, the other rows wait for them.
    const billingBack = screen.getByRole("button", {
      name: "Roll back to this… (billing at 5e6f7a8)",
    });
    expect(billingBack).toHaveAttribute("aria-disabled", "true");
    expect(billingBack).toHaveAccessibleDescription(said);
    await user.click(billingBack);
    expect(screen.queryByRole("dialog")).toBeNull();

    versions.release({ status: 200, body: [billing] });
    await waitFor(() => {
      expect(billingBack).not.toHaveAttribute("aria-disabled");
    });
    expect(screen.queryByRole("button", { name: /analytics/u })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Undeploy… (billing from prod)" }),
    ).not.toHaveAttribute("aria-disabled");
  });

  it("keeps the undeployed repository unoffered when the current versions cannot be read again", async () => {
    let undeployed = false;
    const { server, user } = renderTab(
      list([NOW, aDeploy()]),
      "/jobs/deploys?environment=prod",
      () =>
        undeployed
          ? { status: 503, body: { type: "Unavailable", detail: "The server is busy." } }
          : { status: 200, body: [CURRENT] },
    );
    server.route({
      "GET /api/v1/environments/prod/jobs": { status: 200, body: [] },
      "DELETE /api/v1/environments/prod/deployments/analytics": () => {
        undeployed = true;
        return { status: 204 };
      },
    });

    await user.click(await screen.findByRole("button", { name: /^Undeploy…/u }));
    await user.type(await screen.findByRole("textbox"), "analytics");
    await user.click(screen.getByRole("button", { name: "Undeploy" }));

    expect(await screen.findByText(/The current versions could not be read/u)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Roll back|^Undeploy/u })).toBeNull();
  });

  describe("after a re-read of the current versions failed", () => {
    const billingNow = aDeploy({
      id: "d-billing-now",
      repository: "billing",
      project_version_id: "5e6f7081-92a3-4b4c-8d5e-6f708192a3b4",
      resolved_sha: "6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a",
    });
    const billingOld = aDeploy({
      id: "d-billing-old",
      repository: "billing",
      resolved_sha: "5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f",
      project_version_id: "4d5e6f70-8192-4a3b-9c4d-5e6f708192a3",
      created_at: "2026-11-15T09:00:00Z",
    });
    const billing = aDeployment({
      repository: "billing",
      project_version_id: billingNow.project_version_id ?? "",
      commit_sha: billingNow.resolved_sha ?? "",
    });

    /**
     * Renders the tab over `analytics` and `billing` deployed in `prod`, whose current versions
     * fail to be read once anything was undeployed, and undeploys `analytics`.
     *
     * @returns The server and a user-event session.
     */
    async function undeployAnalytics() {
      let changed = false;
      const rendered = renderTab(
        list([NOW, aDeploy(), billingNow, billingOld]),
        "/jobs/deploys?environment=prod",
        () =>
          changed
            ? { status: 503, body: { type: "Unavailable", detail: "The server is busy." } }
            : { status: 200, body: [CURRENT, billing] },
      );
      const undeployed = (): FakeAnswer => {
        changed = true;
        return { status: 204 };
      };
      rendered.server.route({
        "GET /api/v1/environments/prod/jobs": { status: 200, body: [] },
        "DELETE /api/v1/environments/prod/deployments/analytics": undeployed,
        "DELETE /api/v1/environments/prod/deployments/billing": undeployed,
        "GET /api/v1/environments/prod/deploys/d-rollback": { status: 200, body: billingOld },
        [`GET /api/v1/environments/prod/deploys/${billingOld.id}`]: {
          status: 200,
          body: { ...billingOld, jobs: [] },
        },
        "POST /api/v1/environments/prod/deploys": {
          status: 202,
          body: { id: "d-rollback", status: "queued" },
        },
      });
      const { user } = rendered;
      await user.click(
        await screen.findByRole("button", { name: "Undeploy… (analytics from prod)" }),
      );
      await user.type(await screen.findByRole("textbox"), "analytics");
      await user.click(screen.getByRole("button", { name: "Undeploy" }));
      expect(
        await screen.findByText(/The current versions could not be read/u),
      ).toBeInTheDocument();
      return rendered;
    }

    it("a second undeploy keeps the first repository gone", async () => {
      const { user } = await undeployAnalytics();

      await user.click(screen.getByRole("button", { name: "Undeploy… (billing from prod)" }));
      await user.type(await screen.findByRole("textbox"), "billing");
      await user.click(screen.getByRole("button", { name: "Undeploy" }));

      expect(
        await screen.findByText("Undeployed billing from prod. Its tables stay, as orphans."),
      ).toBeInTheDocument();
      const region = screen.getByRole("region", { name: "Deploys of prod" });
      expect(region).not.toHaveTextContent("current");
      expect(within(region).queryByRole("button", { name: /^Undeploy|^Roll back/u })).toBeNull();
    });

    it("a rollback of another repository keeps the undeployed one gone", async () => {
      const { user } = await undeployAnalytics();

      await user.click(
        screen.getByRole("button", { name: "Roll back to this… (billing at 5e6f7a8)" }),
      );
      await user.click(await screen.findByRole("button", { name: "Roll back" }));

      expect(await screen.findByText(/^Roll(ing|ed) back billing in prod/u)).toBeInTheDocument();
      expect(
        screen.queryByText("Undeployed analytics from prod. Its tables stay, as orphans."),
      ).toBeNull();
      expect(screen.queryByRole("button", { name: /analytics/u })).toBeNull();
      expect(screen.getAllByRole("row")[1]).not.toHaveTextContent("current");
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

describe("DeploysTab's sort", () => {
  it("sorts by result on the server and keeps the repository filter", async () => {
    const { server, user } = renderTab(
      list([NOW]),
      "/jobs/deploys?environment=prod&repository=analytics",
    );
    await screen.findByRole("region", { name: /Deploys/u });
    await user.click(screen.getByRole("button", { name: /^Result/u }));
    await waitFor(() => {
      expect(server.sent.at(-1)?.path).toContain("sort=status&order=asc");
    });
    expect(server.sent.at(-1)?.path).toContain("repository=analytics");
    expect(screen.getByTestId("address")).toHaveTextContent("repository=analytics&sort=status");
    expect(screen.getByRole("columnheader", { name: /Result/u })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });
});

describe("DeploysTab's current mark in another order", () => {
  it("marks nothing current while a later page could hold a newer deploy", async () => {
    renderTab(list([NOW], "next"), "/jobs/deploys?environment=prod&sort=status");
    const region = await screen.findByRole("region", { name: /Deploys/u });
    await within(region).findAllByRole("row");
    expect(region).not.toHaveTextContent("current");
  });

  it("marks nothing current while the rows of the previous sort wait for the new order", async () => {
    const next = held();
    const route: FakeRoute = (request) =>
      request.path.includes("sort=status") ? next.route() : list([NOW]);
    const { user } = renderTab(route, "/jobs/deploys?environment=prod");
    const region = await screen.findByRole("region", { name: /Deploys/u });
    await waitFor(() => {
      expect(region).toHaveTextContent("current");
    });

    await user.click(screen.getByRole("button", { name: /^Result/u }));
    await waitFor(() => {
      expect(region).not.toHaveTextContent("current");
    });
    next.release(list([NOW]));
    await waitFor(() => {
      expect(region).toHaveTextContent("current");
    });
  });

  it("keeps the mark while Show more reads in the newest-first order", async () => {
    const more = held();
    const route: FakeRoute = (request) =>
      request.path.includes("cursor=d2") ? more.route() : list([NOW], "d2");
    const { user } = renderTab(route, "/jobs/deploys?environment=prod");
    const region = await screen.findByRole("region", { name: /Deploys/u });
    await waitFor(() => {
      expect(region).toHaveTextContent("current");
    });

    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(region).toHaveTextContent("current");
    more.release(list([aDeploy({ id: "d-old", created_at: "2026-11-01T08:00:00Z" })]));
    await waitFor(() => {
      expect(within(region).getAllByRole("row")).toHaveLength(3);
    });
    expect(region).toHaveTextContent("current");
  });
});
