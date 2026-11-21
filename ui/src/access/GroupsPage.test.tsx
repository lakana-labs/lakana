import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { areasFor } from "../shell/areas";
import { accessStatusRoute } from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";

const PATH = "/settings/access/groups";

/**
 * The routes of Groups with two groups and no managed scope.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function groupRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...accessStatusRoute(),
    "GET /api/v1/groups": { status: 200, body: [{ name: "analysts" }, { name: "ops" }] },
    ...more,
  };
}

describe("GroupsPage", () => {
  it("lists the groups, each opening its page", async () => {
    renderShell(PATH, { routes: groupRoutes() });

    const region = await screen.findByRole("region", { name: "Groups" });
    expect(within(region).getByRole("link", { name: "analysts" })).toHaveAttribute(
      "href",
      "/settings/access/groups/analysts",
    );
    expect(within(region).getByRole("link", { name: "ops" })).toHaveAttribute(
      "href",
      "/settings/access/groups/ops",
    );
    expect(within(region).getAllByText("›")).toHaveLength(2);
  });

  it("counts the groups and reads them again on Refresh", async () => {
    const { server, user } = renderShell(PATH, { routes: groupRoutes() });
    await screen.findByRole("region", { name: "Groups" });
    expect(await screen.findByText("2 groups")).toHaveClass("toolbar-count");
    const reads = (): number =>
      server.sent.filter((request) => request.path.startsWith("/api/v1/groups?")).length;
    const before = reads();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(reads()).toBeGreaterThan(before);
    });
  });

  it("a new search drops the old rows at once", async () => {
    const answer = held();
    const { user, router, server } = renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/groups": (request) =>
          request.path.includes("q=zz")
            ? answer.route()
            : { status: 200, body: [{ name: "analysts" }] },
      }),
    });
    await screen.findByRole("link", { name: "analysts" });

    await user.type(screen.getByRole("searchbox", { name: "Find groups" }), "zz");

    expect(await screen.findByText("Reading the groups…")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?q=zz");
    expect(screen.queryByRole("link", { name: "analysts" })).toBeNull();
    answer.release({ status: 200, body: [] });
    expect(await screen.findByText("No group matches “zz”.")).toBeInTheDocument();
    expect(server.sent.at(-1)?.path).toBe("/api/v1/groups?q=zz&limit=50");
  });

  it("starts from the words in the address", async () => {
    const { server } = renderShell(`${PATH}?q=ops`, { routes: groupRoutes() });

    expect(await screen.findByRole("searchbox", { name: "Find groups" })).toHaveValue("ops");
    await screen.findByRole("link", { name: "ops" });
    expect(server.sent.some((request) => request.path === "/api/v1/groups?q=ops&limit=50")).toBe(
      true,
    );
  });

  it("clearing the search lists every group again", async () => {
    const { user, router } = renderShell(`${PATH}?q=ops`, {
      routes: groupRoutes({
        "GET /api/v1/groups": (request) => ({
          status: 200,
          body: request.path.includes("q=ops") ? [{ name: "ops" }] : [{ name: "analysts" }],
        }),
      }),
    });
    const field = await screen.findByRole("searchbox", { name: "Find groups" });
    await screen.findByRole("link", { name: "ops" });

    await user.clear(field);

    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    expect(await screen.findByRole("link", { name: "analysts" })).toBeInTheDocument();
  });

  it("keeps the dialog open while the group is being created", async () => {
    const answer = held();
    const { user } = renderShell(PATH, {
      routes: groupRoutes({ "POST /api/v1/groups": answer.route }),
    });

    await user.click(await screen.findByRole("button", { name: "Create group" }));
    const dialog = await screen.findByRole("dialog", { name: "Create group" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "finance");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await user.keyboard("{Escape}");

    expect(screen.getByRole("dialog", { name: "Create group" })).toBeInTheDocument();
    answer.release({ status: 201, body: { name: "finance" } });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("shows more with the next cursor", async () => {
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "GET /api/v1/groups": (request) =>
          request.path.includes("cursor=c2")
            ? { status: 200, body: [{ name: "ops" }] }
            : { status: 200, body: [{ name: "analysts" }], headers: { "X-Next-Cursor": "c2" } },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    expect(await screen.findByRole("link", { name: "ops" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "analysts" })).toBeInTheDocument();
  });

  it("says when there is no group yet", async () => {
    renderShell(PATH, { routes: groupRoutes({ "GET /api/v1/groups": { status: 200, body: [] } }) });

    expect(await screen.findByText("No group yet.")).toBeInTheDocument();
  });

  it("creates a group and puts the focus on the heading", async () => {
    let created = false;
    const { user, server } = renderShell(PATH, {
      routes: groupRoutes({
        "POST /api/v1/groups": () => {
          created = true;
          return { status: 201, body: { name: "finance" } };
        },
        "GET /api/v1/groups": () => ({
          status: 200,
          body: created ? [{ name: "finance" }] : [{ name: "analysts" }],
        }),
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Create group" }));
    const dialog = await screen.findByRole("dialog", { name: "Create group" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "finance");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("link", { name: "finance" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Groups" })).toHaveFocus();
    });
    expect(screen.getByText("Created finance.")).toBeInTheDocument();
    expect(
      server.sent.filter((request) => request.method !== "GET").map((request) => request.path),
    ).toEqual(["/api/v1/groups"]);
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
  });

  it("keeps the dialog with the server's sentence on a taken name", async () => {
    const { user } = renderShell(PATH, {
      routes: groupRoutes({
        "POST /api/v1/groups": {
          status: 409,
          body: { detail: "A group named 'ops' already exists.", type: "AlreadyExistsError" },
        },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Create group" }));
    const dialog = await screen.findByRole("dialog", { name: "Create group" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "ops");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "A group named 'ops' already exists.",
    );
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("ops");

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a managed instance offers no create", async () => {
    renderShell(PATH, { routes: groupRoutes(accessStatusRoute(["instance"])) });

    expect(
      await screen.findByText(/The whole instance is managed by access.yaml/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/lakana group add-member GROUP PRINCIPAL --override-managed/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create group" })).toBeNull();
    expect(await screen.findByRole("link", { name: "analysts" })).toBeInTheDocument();
  });

  it("offers no write until the managed scopes are read", async () => {
    const status = held();
    renderShell(PATH, {
      routes: groupRoutes({ "GET /api/v1/access/status": () => status.route() }),
    });

    expect(await screen.findByRole("link", { name: "analysts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create group" })).toBeNull();
    status.release({ status: 200, body: { scopes: [], overrides: [] } });
    expect(await screen.findByRole("button", { name: "Create group" })).toBeInTheDocument();
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

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "nothing can be changed here for now: The database is down.",
    );
    expect(screen.queryByRole("button", { name: "Create group" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: "Create group" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Groups" })).toHaveFocus();
  });

  it("a non-administrator cannot use Groups", async () => {
    const { server } = renderShell(PATH, {
      access: areasFor(false),
      me: { name: "kim", admin: false },
      routes: groupRoutes(),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Groups" }),
    ).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.startsWith("/api/v1/groups"))).toEqual([]);
  });
});
