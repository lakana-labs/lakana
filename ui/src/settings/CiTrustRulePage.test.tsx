import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { accessStatusRoute } from "../test/access";
import { aDevRule, aRule, ciTrustRoutes, DEV_RULE, PROD_RULE } from "../test/ciTrust";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import { withoutSessionReads } from "../test/server";

const PATH = `/settings/ci-trust/${PROD_RULE}`;
const API = `/api/v1/ci-trust-rules/${PROD_RULE}`;

/**
 * The writes a test sent, without the session reads of the touch.
 *
 * @param server - The fake server.
 * @param server.sent - What it was sent.
 * @returns Each write as `METHOD path`.
 */
function writes(server: Parameters<typeof withoutSessionReads>[0]): string[] {
  return withoutSessionReads(server)
    .filter((request) => request.method !== "GET")
    .map((request) => `${request.method} ${request.path}`);
}

describe("CiTrustRulePage", () => {
  it("shows what the rule lets deploy, as whom, and the claims it needs", async () => {
    renderShell(PATH, { routes: ciTrustRoutes() });

    expect(
      await screen.findByRole("heading", { level: 1, name: "analytics to prod" }),
    ).toBeInTheDocument();
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(trail).getByRole("link", { name: "CI trust" })).toHaveAttribute(
      "href",
      "/settings/ci-trust",
    );
    expect(screen.getByText("GitHub Actions")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "prod" })).toHaveAttribute(
      "href",
      "/settings/environments/prod",
    );
    expect(screen.getByRole("link", { name: "analytics" })).toHaveAttribute(
      "href",
      "/settings/repositories/analytics",
    );
    expect(screen.getByRole("link", { name: "ci-prod" })).toHaveAttribute(
      "href",
      "/settings/access/people/ci-prod",
    );
    expect(screen.getByText(/ci-prod also needs DEPLOY on prod/)).toBeInTheDocument();
    expect(screen.getByText(/starts no job/)).toBeInTheDocument();
    expect(screen.getByText(/by admin/)).toBeInTheDocument();
    expect(screen.getByText(PROD_RULE)).toBeInTheDocument();
    const claims = screen.getByRole("region", { name: "The rule's claims" });
    expect(within(claims).getByText("repository_id").closest("tr")).toHaveTextContent("123456");
    expect(within(claims).getByText("event_name").closest("tr")).toHaveTextContent("push");
    expect(
      screen.getByRole("link", { name: "Deploy from GitHub Actions without a stored secret" }),
    ).toHaveAttribute("href", expect.stringContaining("how-to/deploy-from-github-actions/"));
    expect(screen.getByText("Enabled")).toHaveClass("pill-neutral");
  });

  it("names a deleted creator, who changed it last, and the GitLab how-to", async () => {
    renderShell(`/settings/ci-trust/${DEV_RULE}`, {
      routes: ciTrustRoutes({
        [`GET /api/v1/ci-trust-rules/${DEV_RULE}`]: {
          status: 200,
          body: aDevRule({
            issuer: "https://gitlab.corp.example",
            created_by: null,
            start_on_deploy_jobs: true,
          }),
        },
      }),
    });

    expect(await screen.findByText(/by a deleted account/)).toBeInTheDocument();
    expect(screen.getByText(/by maria/)).toBeInTheDocument();
    expect(screen.getByText("https://gitlab.corp.example")).toBeInTheDocument();
    expect(screen.getByText(/starts the project's jobs with on_deploy: true in dev/)).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Deploy from GitLab CI without a stored secret" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toHaveClass("pill-warn");
  });

  it("says when there is no such rule, with the way back", async () => {
    renderShell(PATH, {
      routes: ciTrustRoutes({
        [`GET ${API}`]: { status: 404, body: { detail: `No CI trust rule ${PROD_RULE}.` } },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(`No CI trust rule ${PROD_RULE}.`);
    expect(screen.getByRole("link", { name: "Back to CI trust" })).toHaveAttribute(
      "href",
      "/settings/ci-trust",
    );
  });

  it("disables after asking, with the touch first, and puts the focus on Enable", async () => {
    let enabled = true;
    const { server, user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        [`GET ${API}`]: () => ({ status: 200, body: aRule({ enabled }) }),
        [`POST ${API}/disable`]: () => {
          enabled = false;
          return { status: 200, body: aRule({ enabled }) };
        },
        [`POST ${API}/enable`]: () => {
          enabled = true;
          return { status: 200, body: aRule({ enabled }) };
        },
      }),
    });

    await user.click(
      await screen.findByRole("button", { name: "Disable the rule for analytics to prod" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Disable the rule for analytics to prod?",
    });
    expect(dialog).toHaveTextContent("the tokens issued under it end now");
    await user.click(within(dialog).getByRole("button", { name: "Disable" }));

    const enable = await screen.findByRole("button", {
      name: "Enable the rule for analytics to prod",
    });
    await waitFor(() => {
      expect(enable).toHaveFocus();
    });
    expect(screen.getByText("Disabled. No CI job is accepted under it.")).toBeInTheDocument();

    await user.click(enable);
    expect(
      await screen.findByText("Enabled. CI jobs that match are accepted again."),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Disable the rule for analytics to prod" }),
      ).toHaveFocus();
    });
    expect(writes(server)).toEqual([`POST ${API}/disable`, `POST ${API}/enable`]);
    expect(
      server.sent.filter((request) => request.path === "/api/v1/sign-in/session"),
    ).toHaveLength(2);
  });

  it("keeps the server's sentence when Enable is refused", async () => {
    const { user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        [`GET ${API}`]: { status: 200, body: aRule({ enabled: false }) },
        [`POST ${API}/enable`]: {
          status: 422,
          body: {
            type: "ValidationError",
            detail: "A GitHub rule must also pin how the run was started.",
          },
        },
      }),
    });

    await user.click(
      await screen.findByRole("button", { name: "Enable the rule for analytics to prod" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A GitHub rule must also pin how the run was started.",
    );
  });

  it("edits only what changed, after the touch", async () => {
    const answer = held();
    const { server, user } = renderShell(PATH, {
      routes: ciTrustRoutes({ [`PATCH ${API}`]: answer.route }),
    });

    await user.click(
      await screen.findByRole("button", { name: "Edit the rule for analytics to prod" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Edit the rule for analytics to prod",
    });
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(within(dialog).getByText("Nothing has changed yet.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("checkbox", { name: "Dry run" }));
    await user.click(within(dialog).getByRole("button", { name: "Remove the claim environment" }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    answer.release({ status: 200, body: aRule({ kinds: ["deploy", "dry_run"] }) });

    expect(
      await screen.findByText("Saved. The tokens issued under the rule before have ended."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    const patch = withoutSessionReads(server).find((request) => request.method === "PATCH");
    expect(patch?.body).toEqual({
      kinds: ["deploy", "dry_run"],
      conditions: { repository_id: "123456", ref: "refs/heads/main", event_name: "push" },
    });
  });

  it("holds Save while a claim has no text, and keeps the server's refusal", async () => {
    const { user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        [`PATCH ${API}`]: {
          status: 404,
          body: { type: "NotFoundError", detail: "No principal named 'ci-pord'." },
        },
      }),
    });

    await user.click(
      await screen.findByRole("button", { name: "Edit the rule for analytics to prod" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Add a claim" }));
    expect(
      within(dialog).getByText("Give every claim a name and the text it must equal."),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Remove claim 5" }));
    const who = within(dialog).getByRole("combobox", { name: "Acts as" });
    await user.clear(who);
    await user.type(who, "ci-pord");
    await user.keyboard("{Escape}");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "No principal named 'ci-pord'.",
    );
    expect(within(dialog).getByRole("combobox", { name: "Acts as" })).toHaveValue("ci-pord");
  });

  it("deletes after asking, and the list says so", async () => {
    let gone = false;
    const { server, user, router } = renderShell(PATH, {
      routes: ciTrustRoutes({
        [`DELETE ${API}`]: () => {
          gone = true;
          return { status: 204 };
        },
        "GET /api/v1/ci-trust-rules": () => ({
          status: 200,
          body: gone ? [aDevRule()] : [aDevRule(), aRule()],
        }),
      }),
    });

    await user.click(
      await screen.findByRole("button", { name: "Delete rule for analytics to prod" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Delete the rule for analytics to prod?",
    });
    await user.click(within(dialog).getByRole("button", { name: "Delete rule" }));

    expect(await screen.findByText("Deleted the rule for analytics to prod.")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/ci-trust");
    expect(screen.queryByRole("link", { name: "analytics to prod" })).toBeNull();
    expect(writes(server)).toEqual([`DELETE ${API}`]);
  });

  it("a managed environment offers no change and names the emergency command", async () => {
    renderShell(PATH, { routes: ciTrustRoutes(accessStatusRoute(["prod"])) });

    expect(
      await screen.findByText(/The environment prod is managed by access.yaml/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(`lakana ci-trust disable ${PROD_RULE} --override-managed --reason "WHY"`),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit the rule/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Disable the rule/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Delete rule for/ })).toBeNull();
  });

  it("says why nothing can be changed when the managed scopes cannot be read", async () => {
    let tries = 0;
    const { user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        "GET /api/v1/access/status": () => {
          tries += 1;
          return tries === 1
            ? { status: 500, body: { detail: "The database is down." } }
            : { status: 200, body: { scopes: [], overrides: [] } };
        },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The database is down.");
    expect(screen.queryByRole("button", { name: /Edit the rule/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /Edit the rule/ })).toBeInTheDocument();
  });
});
