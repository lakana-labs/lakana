import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  accessStatusRoute,
  aGrant,
  anEnvironment,
  aPersonGrant,
  freshSession,
  STALE_SESSION,
  STEP_UP_ROUTES,
} from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const PATH = "/settings/access/grants";
const LAST_ADMIN =
  "This would leave the instance without an active administrator. Give another person the ADMIN role on the instance first, then try again.";

/**
 * The routes of Grants: dev and prod, nothing managed.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function grantRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...accessStatusRoute(),
    "GET /api/v1/sign-in/session": freshSession(),
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), anEnvironment("prod")],
    },
    "GET /api/v1/grants": { status: 200, body: [aGrant()] },
    ...more,
  };
}

/**
 * The request paths to the grants list.
 *
 * @param sent - The requests.
 * @returns Their paths.
 */
function listReads(sent: readonly { method: string; path: string }[]): string[] {
  return sent
    .filter((request) => request.method === "GET" && request.path.startsWith("/api/v1/grants"))
    .map((request) => request.path);
}

describe("GrantsPage", () => {
  it("reads the filter from the address", async () => {
    const { server } = renderShell(`${PATH}?principal=maria&role=ANALYST`, {
      routes: grantRoutes(),
    });

    await screen.findByRole("region", { name: "Grants" });
    expect(listReads(server.sent)).toEqual([
      "/api/v1/grants?principal_name=maria&role=ANALYST&limit=50",
    ]);
    expect(screen.getByRole("button", { name: /^A person/ })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("maria");
    expect(screen.getByRole("button", { name: /^ANALYST/ })).toBeInTheDocument();
  });

  it("counts the grants and reads them again on Refresh", async () => {
    const { server, user } = renderShell(PATH, { routes: grantRoutes() });
    await screen.findByRole("region", { name: "Grants" });
    expect(await screen.findByText(/^\d+ grants?$/)).toHaveClass("toolbar-count");
    const before = listReads(server.sent).length;

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(listReads(server.sent).length).toBeGreaterThan(before);
    });
  });

  it("a new filter drops the old rows and the old error at once", async () => {
    const anyone = held();
    const { user, server, router } = renderShell(`${PATH}?principal=nobody`, {
      routes: grantRoutes({
        "GET /api/v1/grants": (request) =>
          request.path.includes("principal_name=nobody")
            ? {
                status: 404,
                body: { detail: "No principal named 'nobody'.", type: "NotFoundError" },
              }
            : anyone.route(),
      }),
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("No principal named 'nobody'.");

    await user.click(screen.getByRole("button", { name: /Who$/ }));
    await user.click(await screen.findByRole("option", { name: "Anyone" }));

    expect(screen.queryByText("No principal named 'nobody'.")).toBeNull();
    expect(screen.getByText("Reading the grants…")).toBeInTheDocument();
    anyone.release({ status: 200, body: [aGrant()] });
    expect(await screen.findByRole("link", { name: "analysts" })).toBeInTheDocument();
    expect(listReads(server.sent).at(-1)).toBe("/api/v1/grants?limit=50");

    // Back: the 404 again, and no old row above its sentence.
    await router.navigate(-1);
    expect(await screen.findByText("No principal named 'nobody'.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "analysts" })).toBeNull();
  });

  it("shows each grant's who, what, where and when", async () => {
    renderShell(PATH, {
      routes: grantRoutes({
        "GET /api/v1/grants": {
          status: 200,
          body: [
            aGrant({ namespace: "sales", kind: "NAMESPACE" }),
            aPersonGrant("maria", {
              id: "g-2",
              role: null,
              action: "MANAGE_TOKEN",
              kind: "INSTANCE",
              environment: null,
              note: "MANAGE_TOKEN has no effect without the ADMIN role.",
            }),
            aGrant({
              id: "g-3",
              group: null,
              group_id: null,
              principal: { id: "ci", name: "ci-bot", principal_type: "service" },
            }),
            aGrant({ id: "g-4", group: null, group_id: null, principal: null }),
          ],
        },
      }),
    });

    const region = await screen.findByRole("region", { name: "Grants" });
    const [, group, person, service, gone] = within(region).getAllByRole("row");
    expect(within(group as HTMLElement).getByRole("link", { name: "analysts" })).toHaveAttribute(
      "href",
      "/settings/access/groups/analysts",
    );
    expect(group).toHaveTextContent("analystsGroupANALYSTdev › sales");
    expect(within(person as HTMLElement).getByRole("link", { name: "maria" })).toHaveAttribute(
      "href",
      "/settings/access/people/maria",
    );
    expect(person).toHaveTextContent(
      "mariaPersonManage API tokensMANAGE_TOKEN has no effect without the ADMIN role.The whole instance",
    );
    expect(service).toHaveTextContent("ci-botService account");
    expect(gone).toHaveTextContent("a principal no longer named");
  });

  it("marks the grants of a managed environment and offers no remove there", async () => {
    renderShell(PATH, {
      routes: grantRoutes({
        ...accessStatusRoute(["prod"]),
        "GET /api/v1/grants": {
          status: 200,
          body: [aGrant(), aGrant({ id: "g-2", environment: "prod" })],
        },
      }),
    });

    const region = await screen.findByRole("region", { name: "Grants" });
    await within(region).findByRole("button", { name: /^Remove/ });
    const [, dev, prod] = within(region).getAllByRole("row");
    expect(within(dev as HTMLElement).getByRole("button", { name: /^Remove/ })).toBeInTheDocument();
    expect(prod).toHaveTextContent("⌂ GitManaged by access.yaml");
    expect(within(prod as HTMLElement).queryByRole("button")).toBeNull();
  });

  it("a managed instance offers no write at all", async () => {
    const { server } = renderShell(PATH, {
      routes: grantRoutes(accessStatusRoute(["instance"])),
    });

    expect(
      await screen.findByText('lakana grant revoke GRANT_ID --override-managed --reason "WHY"'),
    ).toBeInTheDocument();
    const region = await screen.findByRole("region", { name: "Grants" });
    expect(region).toHaveTextContent("⌂ Git");
    expect(within(region).queryByRole("button", { name: /^Remove/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add grant" })).toBeNull();
    expect(server.sent.some((request) => request.path.includes("override_managed"))).toBe(false);
  });

  it("offers no remove until the managed scopes are read", async () => {
    const status = held();
    renderShell(PATH, {
      routes: grantRoutes({ "GET /api/v1/access/status": status.route }),
    });

    expect(await screen.findByRole("link", { name: "analysts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove/ })).toBeNull();
    status.release({ status: 200, body: { scopes: [], overrides: [] } });
    expect(await screen.findByRole("button", { name: /^Remove/ })).toBeInTheDocument();
  });

  it("says why nothing can be changed when the managed scopes cannot be read", async () => {
    let tries = 0;
    const { user } = renderShell(PATH, {
      routes: grantRoutes({
        "GET /api/v1/access/status": () => {
          tries += 1;
          return tries === 1
            ? { status: 500, body: { detail: "The database is down." } }
            : { status: 200, body: { scopes: [], overrides: [] } };
        },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The database is down.");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /^Remove/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Grants" })).toHaveFocus();
  });

  it("removes after the passkey touch, asked first", async () => {
    fakeWebAuthn();
    let removed = false;
    const { user, server } = renderShell(PATH, {
      routes: grantRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "DELETE /api/v1/grants/g-1": () => {
          removed = true;
          return { status: 204 };
        },
        "GET /api/v1/grants": () => ({
          status: 200,
          body: removed ? [aGrant({ id: "g-2", environment: "prod" })] : [aGrant()],
        }),
      }),
    });

    await user.click(
      await screen.findByRole("button", { name: "Remove the grant ANALYST on dev for analysts" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Remove this grant?" });
    expect(dialog).toHaveTextContent("analysts loses ANALYST on dev.");
    const from = server.sent.length;
    await user.click(within(dialog).getByRole("button", { name: "Remove the grant" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await waitFor(() => {
      expect(server.sent.length).toBeGreaterThanOrEqual(from + 5);
    });
    expect(
      server.sent.slice(from, from + 5).map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/grants/g-1",
      "GET /api/v1/grants?limit=50",
    ]);
    expect(screen.getByText("Removed the grant.")).toBeInTheDocument();
  });

  it("the focus goes to the heading when the last grant goes", async () => {
    let removed = false;
    const { user } = renderShell(PATH, {
      routes: grantRoutes({
        "DELETE /api/v1/grants/g-1": () => {
          removed = true;
          return { status: 204 };
        },
        "GET /api/v1/grants": () => ({ status: 200, body: removed ? [] : [aGrant()] }),
      }),
    });

    await user.click(await screen.findByRole("button", { name: /^Remove/ }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove the grant" }));

    expect(await screen.findByText("No grant yet.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Grants" })).toHaveFocus();
    });
  });

  it("the last administrator's grant stays with the next step", async () => {
    const { user } = renderShell(PATH, {
      routes: grantRoutes({
        "DELETE /api/v1/grants/g-1": {
          status: 409,
          body: { detail: LAST_ADMIN, type: "ConflictError" },
        },
      }),
    });
    const remove = await screen.findByRole("button", { name: /^Remove/ });

    await user.click(remove);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove the grant" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(LAST_ADMIN);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(remove).toHaveFocus();
    });
  });

  it("shows more with the next cursor", async () => {
    const { user } = renderShell(PATH, {
      routes: grantRoutes({
        "GET /api/v1/grants": (request) =>
          request.path.includes("cursor=c2")
            ? { status: 200, body: [aGrant({ id: "g-2", group: "ops" })] }
            : { status: 200, body: [aGrant()], headers: { "X-Next-Cursor": "c2" } },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    expect(await screen.findByRole("link", { name: "ops" })).toBeInTheDocument();
  });

  it("Clear filters goes back to every grant", async () => {
    const { user, router } = renderShell(`${PATH}?group=ops`, {
      routes: grantRoutes({
        "GET /api/v1/grants": (request) => ({
          status: 200,
          body: request.path.includes("group_name") ? [] : [aGrant()],
        }),
      }),
    });

    expect(await screen.findByText("No grant matches these filters.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));

    expect(await screen.findByRole("link", { name: "analysts" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
    expect(screen.queryByRole("textbox", { name: "Name" })).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Grants" })).toHaveFocus();
  });

  it("a non-administrator cannot use Grants", async () => {
    const { server } = renderShell(PATH, {
      me: { name: "kim", admin: false },
      routes: grantRoutes(),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Grants" }),
    ).toBeInTheDocument();
    expect(listReads(server.sent)).toEqual([]);
  });

  it("sends no override", async () => {
    const { user, server } = renderShell(PATH, {
      routes: grantRoutes({ "DELETE /api/v1/grants/g-1": { status: 204 } }),
    });

    await user.click(await screen.findByRole("button", { name: /^Remove/ }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove the grant" }),
    );
    await waitFor(() => {
      expect(server.sent.some((request) => request.method === "DELETE")).toBe(true);
    });
    expect(server.sent.some((request) => request.path.includes("override_managed"))).toBe(false);
  });
});
