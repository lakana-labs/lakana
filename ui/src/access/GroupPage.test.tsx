import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  accessStatusRoute,
  aGrant,
  aMember,
  freshSession,
  STALE_SESSION,
  STEP_UP_ROUTES,
} from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import { expectServerSorts, sortBy, sortState } from "../test/sorting";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const LAST_ADMIN =
  "This would leave the instance without an active administrator. Give another person the ADMIN role on the instance first, then try again.";

/**
 * The routes of the group `analysts` with two members and one grant.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function groupRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...accessStatusRoute(),
    "GET /api/v1/sign-in/session": freshSession(),
    "GET /api/v1/groups/analysts/members": {
      status: 200,
      body: [aMember("ci-bot", "SERVICE"), aMember("maria")],
    },
    "GET /api/v1/grants": {
      status: 200,
      body: [
        aGrant(),
        aGrant({
          id: "g-2",
          role: null,
          action: "MANAGE_TOKEN",
          kind: "INSTANCE",
          environment: null,
          note: "No effect without the ADMIN role.",
        }),
      ],
    },
    "GET /api/v1/groups": { status: 200, body: [{ name: "analysts" }] },
    "GET /api/v1/principals": { status: 200, body: [] },
    ...more,
  };
}

const PATH = "/settings/access/groups/analysts";

describe("GroupPage", () => {
  it("shows the trail, the members and the group's grants", async () => {
    const { server } = renderShell(PATH, { routes: groupRoutes() });

    const members = await screen.findByRole("region", { name: "Members of analysts" });
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(
      within(trail)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Settings", "Access", "Groups"]);
    expect(within(trail).getByText("analysts")).toHaveAttribute("aria-current", "page");
    expect(within(members).getByRole("link", { name: "maria" })).toHaveAttribute(
      "href",
      "/settings/access/people/maria",
    );
    expect(within(members).getByRole("link", { name: "ci-bot" })).toBeInTheDocument();
    expect(within(members).getByText("Service account")).toBeInTheDocument();
    const grants = await screen.findByRole("region", { name: "Grants of analysts" });
    expect(within(grants).getAllByRole("row")[1]).toHaveTextContent("ANALYSTdev");
    expect(within(grants).getAllByRole("row")[2]).toHaveTextContent(
      "Manage API tokensNo effect without the ADMIN role.The whole instance",
    );
    expect(screen.getByRole("link", { name: "Manage these grants" })).toHaveAttribute(
      "href",
      "/settings/access/grants?group=analysts",
    );
    expect(server.sent.find((request) => request.path.startsWith("/api/v1/grants"))?.path).toBe(
      "/api/v1/grants?group_name=analysts&limit=50",
    );
  });

  it("says when the panel shows only the first page of the group's grants", async () => {
    renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/grants": {
          status: 200,
          body: [aGrant()],
          headers: { "X-Next-Cursor": "c2" },
        },
      }),
    });

    expect(
      await screen.findByText("Showing the first 1. Manage these grants lists them all."),
    ).toBeInTheDocument();
  });

  it("adds a member after the passkey touch, asked first", async () => {
    fakeWebAuthn();
    let added = false;
    const { user, server } = renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "PUT /api/v1/groups/analysts/members/ben": () => {
          added = true;
          return { status: 204 };
        },
        "GET /api/v1/groups/analysts/members": () => ({
          status: 200,
          body: added ? [aMember("ben"), aMember("maria")] : [aMember("maria")],
        }),
      }),
    });
    const field = await screen.findByRole("combobox", { name: "Person or service account" });

    await user.type(field, "ben");
    // The open list of suggestions hides the rest of the page from screen readers, not from
    // the pointer.
    const add = [...document.querySelectorAll("button")].find((b) => b.textContent === "Add");
    await user.click(add as HTMLElement);

    expect(await screen.findByRole("link", { name: "ben" })).toBeInTheDocument();
    expect(field).toHaveValue("");
    expect(field).toHaveFocus();
    // The emptied field opens no list of its own.
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByText("Added ben to analysts.")).toBeInTheDocument();
    const after = server.sent.findIndex((request) => request.path === "/api/v1/sign-in/session");
    expect(
      server.sent.slice(after, after + 5).map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "PUT /api/v1/groups/analysts/members/ben",
      "GET /api/v1/groups/analysts/members",
    ]);
  });

  it("adds a member once, however often the form is sent while it waits", async () => {
    const answer = held();
    const { user, server } = renderShell(PATH, {
      routes: groupRoutes({ "PUT /api/v1/groups/analysts/members/ben": answer.route }),
    });
    const field = await screen.findByRole("combobox", { name: "Person or service account" });

    await user.type(field, "ben{Escape}{Enter}");
    await waitFor(() => {
      expect(server.sent.filter((request) => request.method === "PUT")).toHaveLength(1);
    });
    fireEvent.submit(field.closest("form") as HTMLFormElement);
    await user.type(field, "{Enter}");

    expect(server.sent.filter((request) => request.method === "PUT")).toHaveLength(1);
    answer.release({ status: 204 });
    expect(await screen.findByText("Added ben to analysts.")).toBeInTheDocument();
  });

  it("says when no such person exists", async () => {
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "PUT /api/v1/groups/analysts/members/ben": {
          status: 404,
          body: { detail: "No principal named 'ben'.", type: "NotFoundError" },
        },
      }),
    });
    const field = await screen.findByRole("combobox", { name: "Person or service account" });

    await user.type(field, "ben{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("No principal named 'ben'.");
    expect(field).toHaveValue("ben");
    expect(field).toHaveFocus();
  });

  it("suggests people and service accounts as one types and adds the chosen one", async () => {
    const { user, server } = renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/principals": {
          status: 200,
          body: [
            aMember("ben"),
            { ...aMember("bench-loader", "SERVICE_ACCOUNT"), is_active: false },
          ],
        },
        "PUT /api/v1/groups/analysts/members/bench-loader": { status: 204 },
      }),
    });
    const field = await screen.findByRole("combobox", { name: "Person or service account" });

    await user.type(field, "ben");
    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "benPerson",
      "bench-loaderService account · Deactivated",
    ]);
    await user.click(options[1] as HTMLElement);
    expect(field).toHaveValue("bench-loader");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText("Added bench-loader to analysts.")).toBeInTheDocument();
    const asked = server.sent.filter((request) => request.path.startsWith("/api/v1/principals?"));
    expect(asked.map((request) => request.path)).toEqual(["/api/v1/principals?limit=20&q=ben"]);
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toContain(
      "PUT /api/v1/groups/analysts/members/bench-loader",
    );
  });

  it("says when no principal has that name, and still lets the server judge it", async () => {
    const { user, server } = renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/principals": { status: 200, body: [] },
        "PUT /api/v1/groups/analysts/members/bne": {
          status: 404,
          body: { detail: "No principal named 'bne'.", type: "NotFoundError" },
        },
      }),
    });
    const field = await screen.findByRole("combobox", { name: "Person or service account" });

    await user.type(field, "bne");
    expect(await screen.findByRole("status", { name: "" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("No principal named “bne”. Check the spelling.")).toBeInTheDocument();
    });
    await user.keyboard("{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("No principal named 'bne'.");
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toContain(
      "PUT /api/v1/groups/analysts/members/bne",
    );
  });

  it("removes a member and keeps the focus on Members", async () => {
    let removed = false;
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "DELETE /api/v1/groups/analysts/members/maria": () => {
          removed = true;
          return { status: 204 };
        },
        "GET /api/v1/groups/analysts/members": () => ({
          status: 200,
          body: removed ? [] : [aMember("maria")],
        }),
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Remove maria from analysts" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove maria from analysts?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));

    expect(await screen.findByText("analysts has no member yet.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Members" })).toHaveFocus();
    });
  });

  it("the last administrator stays a member, with the next step", async () => {
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "DELETE /api/v1/groups/analysts/members/maria": {
          status: 409,
          body: { detail: LAST_ADMIN, type: "ConflictError" },
        },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Remove maria from analysts" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove maria from analysts?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(LAST_ADMIN);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("deletes the group after its name is typed", async () => {
    const { user, router, server } = renderShell(PATH, {
      routes: groupRoutes({ "DELETE /api/v1/groups/analysts": { status: 204 } }),
    });

    await user.click(await screen.findByRole("button", { name: "Delete group analysts" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete group analysts?" });
    const confirm = within(dialog).getByRole("button", { name: "Delete group" });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAccessibleDescription("Type the group's name exactly to delete it.");
    await user.type(
      within(dialog).getByRole("textbox", { name: "Type analysts to confirm" }),
      "analyst",
    );
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), "s");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/settings/access/groups");
    });
    expect(router.state.historyAction).toBe("REPLACE");
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(true);
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Groups" })).toHaveFocus();
    });
  });

  it("deletes a group whose name holds a hidden character, typed as its label shows it", async () => {
    const name = "an\u202Ealysts";
    const { user, server } = renderShell(`/settings/access/groups/${encodeURIComponent(name)}`, {
      routes: groupRoutes({
        [`GET /api/v1/groups/${name}/members`]: { status: 200, body: [] },
        [`DELETE /api/v1/groups/${name}`]: { status: 204 },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Delete group anU+202Ealysts" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete group anU+202Ealysts?" });
    await user.type(
      within(dialog).getByRole("textbox", { name: "Type anU+202Ealysts to confirm" }),
      "anU+202Ealysts",
    );
    await user.click(within(dialog).getByRole("button", { name: "Delete group" }));

    await waitFor(() => {
      expect(server.sent.find((request) => request.method === "DELETE")?.path).toBe(
        `/api/v1/groups/${encodeURIComponent(name)}`,
      );
    });
  });

  it("deletes nothing on Enter before the name is typed, nor closes while deleting", async () => {
    const answer = held();
    const { user, server } = renderShell(PATH, {
      routes: groupRoutes({ "DELETE /api/v1/groups/analysts": answer.route }),
    });

    await user.click(await screen.findByRole("button", { name: "Delete group analysts" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete group analysts?" });
    await user.type(within(dialog).getByRole("textbox"), "ana{Enter}");
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(false);

    await user.type(within(dialog).getByRole("textbox"), "lysts{Enter}");
    await waitFor(() => {
      expect(server.sent.some((request) => request.method === "DELETE")).toBe(true);
    });
    await user.keyboard("{Enter}");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Delete group analysts?" })).toBeInTheDocument();
    expect(server.sent.filter((request) => request.method === "DELETE")).toHaveLength(1);
    answer.release({ status: 204 });
  });

  it("keeps the dialog with the server's sentence while grants name the group", async () => {
    const sentence = "Grants still reference the group 'analysts'. Revoke them first.";
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "DELETE /api/v1/groups/analysts": {
          status: 409,
          body: { detail: sentence, type: "ConflictError" },
        },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Delete group analysts" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete group analysts?" });
    await user.type(within(dialog).getByRole("textbox"), "analysts{Enter}");

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(sentence);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a managed instance offers no write on a group", async () => {
    renderShell(PATH, { routes: groupRoutes(accessStatusRoute(["instance"])) });

    expect(await screen.findByRole("region", { name: "Members of analysts" })).toBeInTheDocument();
    expect(screen.getByText(/The whole instance is managed by access.yaml/)).toBeInTheDocument();
    expect(
      screen.getByText(
        'lakana group add-member analysts PRINCIPAL --override-managed --reason "WHY"',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Delete group/ })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Person or service account" })).toBeNull();
  });

  it("says why nothing can be changed when the managed scopes cannot be read", async () => {
    let tries = 0;
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/access/status": () => {
          tries += 1;
          return tries === 1
            ? { status: 500, body: { detail: "The database is down." } }
            : { status: 200, body: { scopes: [], overrides: [] } };
        },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(
      await screen.findByRole("button", { name: "Delete group analysts" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "analysts" })).toHaveFocus();
  });

  it("marks a hidden character in a group's name everywhere", async () => {
    const name = "an‮al";
    const { user } = renderShell(`/settings/access/groups/${encodeURIComponent(name)}`, {
      routes: groupRoutes({ [`GET /api/v1/groups/${name}/members`]: { status: 200, body: [] } }),
    });

    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent("anU+202Eal");
    const trail = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(trail).toHaveTextContent("anU+202Eal");
    const remove = await screen.findByRole("button", { name: "Delete group anU+202Eal" });
    await user.click(remove);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Delete group anU+202Eal?");
    expect(document.body.textContent).not.toContain("‮");
  });

  it("reads the members and the grants again after a failure, keeping the focus", async () => {
    let members = 0;
    let grants = 0;
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/groups/analysts/members": () => {
          members += 1;
          return members === 1
            ? { status: 500, body: { detail: "The members are not there." } }
            : { status: 200, body: [] };
        },
        "GET /api/v1/grants": () => {
          grants += 1;
          return grants === 1
            ? { status: 500, body: { detail: "The grants are not there." } }
            : { status: 200, body: [] };
        },
      }),
    });

    const membersPanel = await screen.findByRole("region", { name: "Members" });
    await user.click(await within(membersPanel).findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("analysts has no member yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Members" })).toHaveFocus();

    const grantsPanel = screen.getByRole("region", { name: "Grants" });
    await user.click(await within(grantsPanel).findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("analysts holds no grant.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Grants" })).toHaveFocus();
  });

  it("an unknown group is not found", async () => {
    renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/groups/analysts/members": {
          status: 404,
          body: { detail: "No group named 'analysts'.", type: "NotFoundError" },
        },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("No group named 'analysts'.");
    expect(screen.getByRole("link", { name: "Back to Groups" })).toHaveAttribute(
      "href",
      "/settings/access/groups",
    );
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "analysts" })).toHaveFocus();
    });
  });
});

describe("GroupPage's sorts", () => {
  it("sorts the members by name in the browser and the grants on the server", async () => {
    const { user, server, router } = renderShell(PATH, { routes: groupRoutes() });
    const members = await screen.findByRole("region", { name: "Members of analysts" });
    const names = (): string[] =>
      within(members)
        .getAllByRole("link")
        .map((link) => link.textContent);
    expect(names()).toEqual(["ci-bot", "maria"]);
    expect(sortState(members, "Name")).toBe("ascending");
    await sortBy(user, members, "Name");
    expect(names()).toEqual(["maria", "ci-bot"]);
    expect(router.state.location.search).toBe("?membersSort=name&membersOrder=desc");

    const grants = await screen.findByRole("region", { name: "Grants of analysts" });
    await expectServerSorts(user, grants, server.sent, "/api/v1/grants", [
      ["What", "sort=what&order=asc"],
      ["On", "sort=on&order=asc"],
    ]);
    // Granted is the grants' own order, the newest first; pressed again, the oldest first.
    await sortBy(user, grants, "Granted");
    await expectServerSorts(user, grants, server.sent, "/api/v1/grants", [
      ["Granted", "sort=granted&order=asc"],
    ]);
    expect(router.state.location.search).toContain("grantsSort=granted&grantsOrder=asc");
  });
});
