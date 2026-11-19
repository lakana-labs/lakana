import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { type AreaAccess, areasFor } from "../shell/areas";
import { renderShell } from "../test/render";
import { type FakeAnswer, whoami } from "../test/server";

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

  it("focuses the heading of a place that loads after the move, not on the first load", async () => {
    // Recovery links is opened by no other test in this file: its lazy chunk must still be
    // loading when the link is chosen (the lazy components are shared across the file).
    renderShell("/settings/status", {
      routes: { "GET /api/v1/recovery-links": { status: 200, body: [] } },
    });
    const status = await screen.findByRole("heading", { level: 1, name: "Status" });
    expect(status).not.toHaveFocus();

    const nav = screen.getByRole("navigation", { name: "Settings places" });
    await userEvent.click(within(nav).getByRole("link", { name: "Recovery links" }));

    const heading = await screen.findByRole("heading", { level: 1, name: "Recovery links" });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
  });

  it.each(["/settings", "/settings/access"])(
    "leaves the focus alone on a direct visit to %s",
    async (path) => {
      renderShell(path, { routes: { "GET /api/v1/principals": { status: 200, body: [] } } });
      const heading = await screen.findByRole("heading", { level: 1 });
      await new Promise((resolve) => {
        setTimeout(resolve, 50);
      });

      expect(heading).not.toHaveFocus();
    },
  );

  it("lists every place for an administrator and marks the current one", async () => {
    renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 404, body: {} } },
    });
    await screen.findByRole("link", { name: "Secrets" });

    expect(await placeNames()).toEqual([
      "Status",
      "Repositories",
      "People",
      "Groups",
      "Grants",
      "Tokens",
      "Secrets",
      "Row filters & masks",
      "Access file",
      "What I may do",
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

  it("/settings/access opens What I may do for a person without admin rights", async () => {
    const { router } = renderShell("/settings/access", {
      access: MEMBER,
      me: { name: "kim", admin: false },
      routes: {
        "GET /api/v1/principals/kim/grants": { status: 200, body: [] },
        "GET /api/v1/environments": { status: 200, body: [] },
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "What I may do" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/access/what-i-may-do");
    expect(await placeNames()).toEqual(["Status", "What I may do"]);
  });

  it("a pending rule shows nothing, then the place", async () => {
    let answer: (value: FakeAnswer) => void = () => undefined;
    const held = new Promise<FakeAnswer>((resolve) => {
      answer = resolve;
    });
    renderShell("/settings/access/secrets", {
      access: MEMBER,
      me: { name: "kim", admin: false },
      routes: { "GET /api/v1/whoami": () => held },
    });
    expect(await placeNames()).toEqual(["Status", "What I may do"]);
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();

    answer({ status: 200, body: whoami("kim", false, true, false, ["dev"]) });

    expect(await screen.findByRole("heading", { level: 1, name: "Secrets" })).toBeInTheDocument();
    expect(await placeNames()).toEqual(["Status", "Secrets", "What I may do"]);
  });

  it.each([
    ["a person who manages no secrets", { status: 200, body: whoami("kim", false) }],
    ["a failed read", { status: 500, body: { detail: "down" } }],
  ])("%s cannot use Secrets", async (_, answer) => {
    const { server } = renderShell("/settings/access/secrets", {
      access: MEMBER,
      me: { name: "kim", admin: false },
      routes: { "GET /api/v1/whoami": answer },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Secrets" }),
    ).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.includes("/secrets"))).toEqual([]);
  });

  it("a policy manager lands on Row filters & masks", async () => {
    let answer: (value: FakeAnswer) => void = () => undefined;
    const held = new Promise<FakeAnswer>((resolve) => {
      answer = resolve;
    });
    const { router } = renderShell("/settings/access", {
      access: MEMBER,
      me: { name: "pat", admin: false },
      routes: { "GET /api/v1/whoami": () => held },
    });
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(router.state.location.pathname).toBe("/settings/access");
    expect(screen.queryByRole("heading", { name: /You cannot use/ })).not.toBeInTheDocument();

    answer({ status: 200, body: whoami("pat", false, true, false, [], true) });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Row filters & masks" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/access/filters-and-masks");
    expect(await placeNames()).toEqual(["Status", "Row filters & masks", "What I may do"]);
  });

  it("a policy manager cannot use Access file", async () => {
    const { server } = renderShell("/settings/access/access-file", {
      access: MEMBER,
      me: { name: "pat", admin: false },
      routes: {
        "GET /api/v1/whoami": { status: 200, body: whoami("pat", false, true, false, [], true) },
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Access file" }),
    ).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.startsWith("/api/v1/access/"))).toEqual([]);
  });

  it("lists Status and What I may do for a person who is not an administrator", async () => {
    renderShell("/settings/status", { access: MEMBER, me: { admin: false } });

    expect(await placeNames()).toEqual(["Status", "What I may do"]);
  });

  it.each([
    ["/settings/repositories/new", "You cannot use Repositories"],
    ["/settings/sign-in", "You cannot use Sign-in"],
  ])("a deep link to a hidden place says so and reads nothing (%s)", async (path, heading) => {
    const { server } = renderShell(path, { access: MEMBER, me: { admin: false } });

    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(await placeNames()).toEqual(["Status", "What I may do"]);
    expect(
      server.sent.filter(
        (request) =>
          request.path.startsWith("/api/v1/settings/sign-in") ||
          request.path.startsWith("/api/v1/repository-probes"),
      ),
    ).toEqual([]);
  });

  it("a deep link in other letter case to a hidden place says so and reads nothing", async () => {
    const { server } = renderShell("/Settings/Sign-in/Invites", {
      access: MEMBER,
      me: { admin: false },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Invites" }),
    ).toBeInTheDocument();
    expect(server.sent.filter((request) => request.path.startsWith("/api/v1/invites"))).toEqual([]);
  });

  it.each([
    ["/settings/%72epositories", "Repositories"],
    ["/Settings/%52epositories/New", "Repositories"],
    ["/settings/%61ccess/people", "People"],
    ["/settings/Access/%50eople/maria", "People"],
    ["/settings/access/%70eople", "People"],
    ["/settings/%73ign-in", "Sign-in"],
    ["/SETTINGS/%53ign-In/", "Sign-in"],
    ["/settings/%73ign-in/invites", "Invites"],
    ["/Settings/Sign-In/%49nvites", "Invites"],
    ["/settings/sign-in/%72ecovery-links", "Recovery links"],
    ["/settings/%53IGN-IN/Recovery-%4Cinks", "Recovery links"],
  ])(
    "an encoded deep link to a hidden place says so and reads nothing (%s)",
    async (path, place) => {
      const { server } = renderShell(path, { access: MEMBER, me: { admin: false } });

      expect(
        await screen.findByRole("heading", { level: 1, name: `You cannot use ${place}` }),
      ).toBeInTheDocument();
      await new Promise((resolve) => {
        setTimeout(resolve, 50);
      });
      expect(
        server.sent
          .map((request) => request.path)
          .filter(
            (sent) =>
              sent === "/api/v1/principals" ||
              [
                "/api/v1/repository-probes",
                "/api/v1/repositories",
                "/api/v1/principals/maria",
                "/api/v1/settings/sign-in",
                "/api/v1/invites",
                "/api/v1/recovery-links",
              ].some((hidden) => sent.startsWith(hidden)),
          ),
      ).toEqual([]);
    },
  );

  it("marks the current place for an encoded address", async () => {
    renderShell("/settings/%73ign-in/invites", {
      routes: { "GET /api/v1/invites": { status: 200, body: [] } },
    });

    expect(await screen.findByRole("heading", { level: 1, name: "Invites" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Settings places" });
    expect(within(nav).getByRole("link", { name: "Invites" })).toHaveAttribute(
      "aria-current",
      "page",
    );
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
