import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Page } from "./Page";
import type { AreaModule } from "./routes";

import { queryRoutes } from "../test/queries";
import { renderShell } from "../test/render";

// The area, its address and the heading it opens on: Settings opens on its first place, Status.
const shippedAreas = [
  ["Home", "/", "Home"],
  ["Catalog", "/catalog", "Catalog"],
  ["Queries", "/queries", "Queries"],
  ["Jobs", "/jobs", "Jobs"],
  ["Connections", "/connections", "Connections"],
  ["Settings", "/settings", "Status"],
] as const;

/**
 * The sidebar's links that mark themselves as the current page (an area's tabs mark theirs too).
 *
 * @returns Their names.
 */
function currentLinks(): string[] {
  const sidebar = screen.getByRole("navigation", { name: "Areas" });
  return within(sidebar)
    .getAllByRole("link")
    .filter((link) => link.getAttribute("aria-current") === "page")
    .map((link) => link.textContent);
}

describe("appRoutes", () => {
  it.each(shippedAreas)("%s has its own URL, heading and title", async (name, path, heading) => {
    renderShell(path);

    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    // A screen loaded on demand renders outside act(), so its title effect may come a moment later.
    await waitFor(() => {
      expect(document.title).toBe(`${heading} · Lakana`);
    });
    expect(currentLinks()).toEqual([name]);
  });

  it("an unknown path shows the not-found state inside the shell", async () => {
    const { user, router } = renderShell("/catalog/a/b/c/d");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Check the address, or start again from Home.")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Areas" })).toBeInTheDocument();
    // The previous test's screen loads on demand; its title effect may land after this render.
    await waitFor(() => {
      expect(document.title).toBe("This page does not exist · Lakana");
    });

    await user.click(screen.getByRole("link", { name: "Go to Home" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/");
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
  });

  it("/experiments is not found", async () => {
    renderShell("/experiments");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });

  it("an area outside the user's access shows the permission state", async () => {
    const { user, router } = renderShell("/jobs", { access: new Set(["home", "catalog"]) });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Jobs" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Ask an administrator if you need it.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Jobs" })).not.toBeInTheDocument();
    expect(document.title).toBe("You cannot use Jobs · Lakana");

    await user.click(screen.getByRole("link", { name: "Go to Home" }));
    expect(router.state.location.pathname).toBe("/");
  });

  it("Home outside the user's access shows the permission state too", async () => {
    renderShell("/", { access: new Set(["catalog"]) });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Home" }),
    ).toBeInTheDocument();
  });

  it("moves focus to the heading after navigation", async () => {
    const { user } = renderShell("/");
    const home = await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(home).not.toHaveFocus();

    await user.click(screen.getByRole("link", { name: "Catalog" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Catalog" })).toHaveFocus();
    expect(currentLinks()).toEqual(["Catalog"]);
  });

  it("the skip link moves focus to the main content", async () => {
    const { user } = renderShell("/queries");
    await screen.findByRole("heading", { level: 1, name: "Queries" });

    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("an area's page can be loaded on demand, in its own chunk", async () => {
    const load = vi.fn(() =>
      Promise.resolve({
        AreaScreen: () => (
          <Page title="Jobs">
            <p>Loaded on demand</p>
          </Page>
        ),
      }),
    );
    const { user } = renderShell("/", { pages: { jobs: load } });
    await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(load).not.toHaveBeenCalled();

    await user.click(screen.getByRole("link", { name: "Jobs" }));

    expect(await screen.findByText("Loaded on demand")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Jobs" })).toHaveFocus();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("a deep link to an on-demand page loads it", async () => {
    let arrive: (module: AreaModule) => void = () => undefined;
    const arriving = new Promise<AreaModule>((resolve) => {
      arrive = resolve;
    });
    renderShell("/jobs", { pages: { jobs: () => arriving } });

    // The shell is there while the screen loads; only the working area waits.
    expect(await screen.findByRole("navigation")).toBeInTheDocument();
    expect(screen.queryByText("Loaded on demand")).not.toBeInTheDocument();
    arrive({ AreaScreen: () => <p>Loaded on demand</p> });

    expect(await screen.findByText("Loaded on demand")).toBeInTheDocument();
  });

  it("/queries lazy-loads its screen, with the SQL editor in a shadow root", async () => {
    renderShell("/queries", { routes: queryRoutes() });

    expect(await screen.findByRole("heading", { level: 1, name: "Queries" })).toBeInTheDocument();
    const host = await waitFor(() => {
      const found = document.querySelector<HTMLElement>(".sql-editor");
      expect(found?.shadowRoot).toBeTruthy();
      return found;
    });
    expect(
      within(host?.shadowRoot as unknown as HTMLElement).getByRole("textbox", { name: "SQL" }),
    ).toBeInTheDocument();
  });

  it("never loads the page of an area outside the user's access", async () => {
    const load = vi.fn(() => Promise.resolve({ AreaScreen: () => <p>Loaded on demand</p> }));
    renderShell("/jobs", { access: new Set(["home"]), pages: { jobs: load } });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Jobs" }),
    ).toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
  });

  describe("the old Access addresses", () => {
    const PEOPLE = {
      "GET /api/v1/principals/kim%20lee": {
        status: 200,
        body: { name: "kim lee", type: "USER", is_active: true, picture: null, held_admin: false },
      },
      "GET /api/v1/principals/kim%20lee/passkeys": { status: 200, body: [] },
      "GET /api/v1/principals/kim%20lee/sessions": { status: 200, body: [] },
    };

    it("keeps the encoding, the search and the hash, and replaces the old entry", async () => {
      const { router } = renderShell("/access/people/kim%20lee?from=mail#passkeys", {
        routes: PEOPLE,
      });

      expect(await screen.findByRole("heading", { level: 1, name: "kim lee" })).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/settings/access/people/kim%20lee");
      expect(router.state.location.search).toBe("?from=mail");
      expect(router.state.location.hash).toBe("#passkeys");
      expect(router.state.historyAction).toBe("REPLACE");
    });

    it("an old address of a hidden place lands on the permission state", async () => {
      const { router, server } = renderShell("/access/invites", { me: { admin: false } });

      expect(
        await screen.findByRole("heading", { level: 1, name: "You cannot use Invites" }),
      ).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/settings/sign-in/invites");
      expect(server.sent.filter((request) => request.path === "/api/v1/invites")).toEqual([]);
    });

    it("a path that only begins with access is not found", async () => {
      const { router } = renderShell("/accessibility");

      expect(
        await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
      ).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/accessibility");
    });

    it.each([
      ["/access", "People"],
      ["/access/recovery-links", "Recovery links"],
    ])("%s lands on %s for an administrator", async (path, heading) => {
      renderShell(path, {
        routes: {
          "GET /api/v1/principals": { status: 200, body: [] },
          "GET /api/v1/recovery-links": { status: 200, body: [] },
        },
      });

      expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    });
  });
});
