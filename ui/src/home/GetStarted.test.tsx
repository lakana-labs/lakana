import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DOCS_URL } from "../shell/docs";

import { STATE } from "../test/home";
import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";

const STEPS = [
  "connect_repository",
  "invite_team",
  "add_passkey",
  "deploy_project",
  "deploy_project_local",
  "ci_deploy",
  "open_table",
  "run_query",
];

/**
 * Renders Home with a Get started list.
 *
 * @param steps - The steps, each with whether it is done.
 * @returns The rendered shell.
 */
function withSteps(steps: { id: string; done: boolean }[]): ReturnType<typeof renderShell> {
  return renderShell("/", {
    routes: {
      "GET /api/v1/home/get-started": {
        status: 200,
        body: { audience: "admin", local: false, steps, hidden: false, complete: false },
      },
    },
  });
}

/**
 * The row of one step.
 *
 * @param text - Part of its sentence.
 * @returns The list item.
 */
function row(text: RegExp): HTMLElement {
  return screen.getByText(text).closest("li") as HTMLElement;
}

describe("GetStarted", () => {
  it.each([
    [
      "connect_repository",
      /Connect the Git repository/,
      "Connect a repository",
      "/settings/repositories/new",
    ],
    ["invite_team", /Invite your colleagues/, "Invite your team", "/access/invites"],
    ["add_passkey", /Add a passkey, so/, "Add a passkey", "/account"],
    ["open_table", /Open a table in the Catalog/, "Open the Catalog", "/catalog"],
    ["run_query", /Run your first query/, "Open Queries", "/queries"],
  ])("goes straight to the place of %s", async (id, text, action, path) => {
    const { user, router } = withSteps([{ id, done: false }]);

    await screen.findByRole("region", { name: "Get started" });
    await user.click(within(row(text)).getByRole("link", { name: action }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(path);
    });
    cleanup();
  });

  it.each([
    ["deploy_project", /Deploy your first project:/, "lakana deploy --env dev"],
    [
      "deploy_project_local",
      /from a folder on this machine/,
      'lakana repo add analytics --kind local --url "$PWD"',
    ],
  ])("offers the tutorial and the command to copy for %s", async (id, text, command) => {
    withSteps([{ id, done: false }]);

    await screen.findByRole("region", { name: "Get started" });
    const item = row(text);
    expect(within(item).getByRole("link", { name: /Open the tutorial/ })).toHaveAttribute(
      "href",
      `${DOCS_URL}tutorials/getting-started/`,
    );
    expect(within(item).getByText(command)).toBeInTheDocument();
    expect(within(item).getByRole("button", { name: "Copy command" })).toBeInTheDocument();
    cleanup();
  });

  it("links CI deploys to their how-to, in a new tab", async () => {
    withSteps([{ id: "ci_deploy", done: false }]);

    await screen.findByRole("region", { name: "Get started" });
    const link = within(row(/Let CI deploy/)).getByRole("link", {
      name: /Deploy from GitHub Actions/,
    });
    expect(link).toHaveAttribute("href", `${DOCS_URL}how-to/deploy-from-github-actions/`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).toHaveTextContent("(opens in a new tab)");
  });

  it("shows the pending sign on Hide while the choice is saved", async () => {
    const request = held();
    const { user } = renderShell("/", {
      routes: {
        "GET /api/v1/home/get-started": {
          status: 200,
          body: {
            audience: "admin",
            local: false,
            steps: [{ id: "invite_team", done: false }],
            hidden: false,
            complete: false,
          },
        },
        "PATCH /api/v1/principals/admin/interface-state": request.route,
      },
    });
    const region = await screen.findByRole("region", { name: "Get started" });

    await user.click(within(region).getByRole("button", { name: "Hide" }));

    expectPending(within(region).getByRole("button", { name: /^Hide/ }));
    request.release({ status: 200, body: { ...STATE, get_started_hidden: true } });
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Get started" })).not.toBeInTheDocument();
    });
  });

  it("marks a done step with the succeeded glyph and the word Done", async () => {
    withSteps(STEPS.map((id) => ({ id, done: id === "invite_team" })));

    const list = await screen.findByRole("region", { name: "Get started" });
    const done = row(/Invite your colleagues/);
    expect(within(done).getByText("Done")).toBeInTheDocument();
    expect(done.querySelector(".status-glyph-succeeded")).not.toBeNull();
    expect(within(done).queryByRole("link")).not.toBeInTheDocument();
    expect(within(row(/Connect the Git repository/)).queryByText("Done")).not.toBeInTheDocument();
    expect(within(list).getAllByRole("listitem")).toHaveLength(STEPS.length);
  });
});
