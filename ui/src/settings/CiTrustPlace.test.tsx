import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { areasFor } from "../shell/areas";
import { accessStatusRoute } from "../test/access";
import { aDevRule, aRule, ciTrustRoutes, DEV_RULE, PROD_RULE } from "../test/ciTrust";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import { withoutSessionReads } from "../test/server";

const PATH = "/settings/ci-trust";

describe("CiTrustPlace", () => {
  it("lists the rules, each opening its page", async () => {
    renderShell(PATH, { routes: ciTrustRoutes() });

    const region = await screen.findByRole("region", { name: "CI trust rules" });
    expect(within(region).getByRole("link", { name: "analytics to prod" })).toHaveAttribute(
      "href",
      `/settings/ci-trust/${PROD_RULE}`,
    );
    expect(within(region).getByRole("link", { name: "analytics to dev" })).toHaveAttribute(
      "href",
      `/settings/ci-trust/${DEV_RULE}`,
    );
    const prod = within(region).getByRole("link", { name: "analytics to prod" }).closest("tr");
    expect(prod).toHaveTextContent("GitHub Actions");
    expect(prod).toHaveTextContent("ci-prod");
    expect(prod).toHaveTextContent("Deploy");
    expect(within(prod as HTMLElement).getByText("Enabled")).toHaveClass("pill-neutral");
    const dev = within(region).getByRole("link", { name: "analytics to dev" }).closest("tr");
    expect(dev).toHaveTextContent("Dry run");
    expect(within(dev as HTMLElement).getByText("Disabled")).toHaveClass("pill-warn");
    expect(screen.getByText("2 rules")).toHaveClass("toolbar-count");
    expect(screen.getByRole("link", { name: "Explain a CI job's claims" })).toHaveAttribute(
      "href",
      "/settings/ci-trust/explain",
    );
  });

  it("says when there is no rule yet", async () => {
    renderShell(PATH, {
      routes: ciTrustRoutes({ "GET /api/v1/ci-trust-rules": { status: 200, body: [] } }),
    });

    expect(await screen.findByText(/No CI trust rule yet/)).toBeInTheDocument();
  });

  it("reads the rules again on Refresh", async () => {
    const { server, user } = renderShell(PATH, { routes: ciTrustRoutes() });
    await screen.findByRole("link", { name: "analytics to prod" });
    const reads = (): number =>
      server.sent.filter((request) => request.path === "/api/v1/ci-trust-rules").length;
    const before = reads();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(reads()).toBeGreaterThan(before);
    });
  });

  it("a non-administrator cannot use CI trust, and nothing is read", async () => {
    const { server } = renderShell(PATH, {
      access: areasFor(false),
      me: { name: "kim", admin: false },
      routes: ciTrustRoutes(),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use CI trust" }),
    ).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.startsWith("/api/v1/ci-trust"))).toEqual(
      [],
    );
  });

  it("a managed instance offers no Add rule and names the emergency command", async () => {
    renderShell(PATH, { routes: ciTrustRoutes(accessStatusRoute(["instance"])) });

    expect(
      await screen.findByText(/The whole instance is managed by access.yaml/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/lakana ci-trust disable RULE_ID --override-managed --reason "WHY"/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add rule" })).toBeNull();
    expect(await screen.findByRole("link", { name: "analytics to prod" })).toBeInTheDocument();
  });

  it("marks the rules of a managed environment", async () => {
    renderShell(PATH, { routes: ciTrustRoutes(accessStatusRoute(["prod"])) });

    const link = await screen.findByRole("link", { name: "analytics to prod" });
    expect(link.closest("tr")).toHaveTextContent("Managed by access.yaml");
    const dev = screen.getByRole("link", { name: "analytics to dev" }).closest("tr");
    expect(dev).not.toHaveTextContent("Managed by");
    expect(screen.getByRole("button", { name: "Add rule" })).toBeInTheDocument();
  });

  it("offers no write until the managed scopes are read", async () => {
    const status = held();
    renderShell(PATH, {
      routes: ciTrustRoutes({ "GET /api/v1/access/status": () => status.route() }),
    });

    expect(await screen.findByRole("link", { name: "analytics to prod" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add rule" })).toBeNull();
    status.release({ status: 200, body: { scopes: [], overrides: [] } });
    expect(await screen.findByRole("button", { name: "Add rule" })).toBeInTheDocument();
  });

  it("says why nothing can be changed when the managed scopes cannot be read", async () => {
    renderShell(PATH, {
      routes: ciTrustRoutes({
        "GET /api/v1/access/status": { status: 500, body: { detail: "The database is down." } },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "nothing can be changed here for now: The database is down.",
    );
    expect(screen.queryByRole("button", { name: "Add rule" })).toBeNull();
  });

  it("adds a rule after the touch, then says so and reads the list again", async () => {
    let added = false;
    const { server, user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        "POST /api/v1/ci-trust-rules": () => {
          added = true;
          return { status: 201, body: aRule({ id: "new-rule", repository: "dbt" }) };
        },
        "GET /api/v1/ci-trust-rules": () => ({
          status: 200,
          body: added
            ? [aDevRule(), aRule(), aRule({ id: "new-rule", repository: "dbt" })]
            : [aDevRule(), aRule()],
        }),
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Add rule" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a CI trust rule" });
    await user.click(within(dialog).getByRole("button", { name: /Environment/ }));
    await user.click(await screen.findByRole("option", { name: /prod/ }));
    await user.click(within(dialog).getByRole("button", { name: /Repository/ }));
    await user.click(await screen.findByRole("option", { name: "dbt" }));
    await user.type(within(dialog).getByRole("combobox", { name: "Acts as" }), "ci-prod");
    await user.keyboard("{Escape}");
    await user.type(within(dialog).getByRole("textbox", { name: "Value of claim 1" }), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Add rule" }));

    expect(await screen.findByText("Added the rule for dbt to prod.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByRole("link", { name: "dbt to prod" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "CI trust" })).toHaveFocus();
    });
    const writes = withoutSessionReads(server).filter((request) => request.method !== "GET");
    expect(writes.map((request) => request.body)).toEqual([
      {
        issuer: "github",
        principal: "ci-prod",
        environment: "prod",
        repository: "dbt",
        kinds: ["deploy"],
        conditions: { repository_id: "123456", ref: "refs/heads/main", event_name: "push" },
        start_on_deploy_jobs: false,
      },
    ]);
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(true);
  });

  it("opens Add rule filled in from the address, as Connect a repository links to it", async () => {
    const { router } = renderShell(`${PATH}?add=1&repository=dbt&environment=dev&issuer=gitlab`, {
      routes: ciTrustRoutes(),
    });

    const dialog = await screen.findByRole("dialog", { name: "Add a CI trust rule" });
    expect(within(dialog).getByRole("radio", { name: "GitLab.com" })).toBeChecked();
    await waitFor(() => {
      expect(within(dialog).getByRole("button", { name: /Environment/ })).toHaveTextContent("dev");
    });
    await waitFor(() => {
      expect(within(dialog).getByRole("button", { name: /Repository/ })).toHaveTextContent("dbt");
    });
    expect(within(dialog).getByRole("textbox", { name: "Claim 1" })).toHaveValue("project_id");
    // The address no longer asks for the dialog, so going back does not open it again.
    expect(router.state.location.search).toBe("");
  });
});
