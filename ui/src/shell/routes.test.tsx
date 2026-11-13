import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Page } from "./Page";
import type { AreaModule } from "./routes";

import { renderShell } from "../test/render";

const shippedAreas = [
  ["Home", "/"],
  ["Catalog", "/catalog"],
  ["Queries", "/queries"],
  ["Jobs", "/jobs"],
  ["Connections", "/connections"],
  ["Access", "/access"],
  ["Settings", "/settings"],
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
  it.each(shippedAreas)("%s has its own URL, heading and title", async (name, path) => {
    renderShell(path);

    expect(await screen.findByRole("heading", { level: 1, name })).toBeInTheDocument();
    // A screen loaded on demand renders outside act(), so its title effect may come a moment later.
    await waitFor(() => {
      expect(document.title).toBe(`${name} · Lakana`);
    });
    expect(currentLinks()).toEqual([name]);
  });

  it("an unknown path shows the not-found state inside the shell", async () => {
    const { user, router } = renderShell("/catalog/does/not/exist");

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Check the address, or start again from Home.")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Areas" })).toBeInTheDocument();
    expect(document.title).toBe("This page does not exist · Lakana");

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

  it("never loads the page of an area outside the user's access", async () => {
    const load = vi.fn(() => Promise.resolve({ AreaScreen: () => <p>Loaded on demand</p> }));
    renderShell("/jobs", { access: new Set(["home"]), pages: { jobs: load } });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Jobs" }),
    ).toBeInTheDocument();
    expect(load).not.toHaveBeenCalled();
  });
});
