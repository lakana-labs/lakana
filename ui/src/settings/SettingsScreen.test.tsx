import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { type AreaAccess, areasFor } from "../shell/areas";
import { renderShell } from "../test/render";

/** Settings for a person who is not an administrator: the area given, the places not. */
const MEMBER: AreaAccess = areasFor(false);

/**
 * The names of the links in Settings' list of places, in order.
 *
 * @returns The names.
 */
async function placeNames(): Promise<string[]> {
  const nav = await screen.findByRole("navigation", { name: "Settings places" });
  return within(nav)
    .getAllByRole("link")
    .map((link) => link.textContent);
}

describe("SettingsScreen", () => {
  it("opens Status at /settings", async () => {
    const { router } = renderShell("/settings");

    expect(await screen.findByRole("heading", { level: 1, name: "Status" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/status");
  });

  it("lists every place for an administrator and marks the current one", async () => {
    renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 404, body: {} } },
    });

    expect(await placeNames()).toEqual([
      "Status",
      "Repositories",
      "People",
      "Sign-in",
      "Invites",
      "Recovery links",
    ]);
    const nav = screen.getByRole("navigation", { name: "Settings places" });
    expect(within(nav).getByRole("link", { name: "Sign-in" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).getByRole("link", { name: "Status" })).not.toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("indents People under Access and the Sign-in places under Sign-in", async () => {
    renderShell("/settings/status");

    const nav = await screen.findByRole("navigation", { name: "Settings places" });
    expect(within(nav).queryByRole("link", { name: "Access" })).not.toBeInTheDocument();
    const access = within(nav).getByText("Access").closest("li");
    if (access === null) throw new Error("Access is not a list item");
    expect(within(access).getByRole("link", { name: "People" })).toBeInTheDocument();
    const signIn = within(nav).getByRole("link", { name: "Sign-in" }).closest("li");
    if (signIn === null) throw new Error("Sign-in is not a list item");
    expect(within(signIn).getByRole("link", { name: "Invites" })).toBeInTheDocument();
    expect(within(signIn).getByRole("link", { name: "Recovery links" })).toBeInTheDocument();
  });

  it("/settings/access opens People for an administrator", async () => {
    const { router } = renderShell("/settings/access", {
      routes: { "GET /api/v1/principals": { status: 200, body: [] } },
    });

    expect(await screen.findByRole("heading", { level: 1, name: "People" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/access/people");
  });

  it("/settings/access with no Access place says so", async () => {
    renderShell("/settings/access", { access: MEMBER, me: { admin: false } });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Access" }),
    ).toBeInTheDocument();
    expect(await placeNames()).toEqual(["Status"]);
  });

  it("lists only Status for a person who is not an administrator", async () => {
    renderShell("/settings/status", { access: MEMBER, me: { admin: false } });

    expect(await placeNames()).toEqual(["Status"]);
  });

  it.each([
    ["/settings/repositories/new", "You cannot use Repositories"],
    ["/settings/sign-in", "You cannot use Sign-in"],
  ])("a deep link to a hidden place says so and reads nothing (%s)", async (path, heading) => {
    const { server } = renderShell(path, { access: MEMBER, me: { admin: false } });

    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(await placeNames()).toEqual(["Status"]);
    expect(
      server.sent.filter(
        (request) =>
          request.path.startsWith("/api/v1/settings/sign-in") ||
          request.path.startsWith("/api/v1/repository-probes"),
      ),
    ).toEqual([]);
  });

  it("an unknown place is not found", async () => {
    renderShell("/settings/nowhere");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });

  it("Connect a repository still opens below Repositories", async () => {
    renderShell("/settings/repositories/new");

    expect(
      await screen.findByRole("heading", { level: 2, name: "1. The repository" }),
    ).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Settings places" });
    expect(within(nav).getByRole("link", { name: "Repositories" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
