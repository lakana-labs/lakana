import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderShell } from "../test/render";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Sidebar", () => {
  it("puts the work areas and the admin areas in two lists", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    const lists = within(screen.getByRole("navigation")).getAllByRole("list");
    expect(
      lists.map((list) =>
        within(list)
          .getAllByRole("link")
          .map((l) => l.textContent),
      ),
    ).toEqual([
      ["Home", "Catalog", "Queries", "Jobs", "Connections"],
      ["Access", "Settings"],
    ]);
  });

  it("leaves out the admin list when no admin area is visible", async () => {
    renderShell("/", { access: new Set(["home"]) });
    await screen.findByRole("heading", { level: 1, name: "Home" });

    expect(within(screen.getByRole("navigation")).getAllByRole("list")).toHaveLength(1);
  });

  it("shows Ctrl K outside Apple platforms", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    const search = screen.getByRole("button", { name: "Search" });
    expect(search).toHaveTextContent("Ctrl K");
    expect(search).toHaveAttribute("aria-keyshortcuts", "Control+K Meta+K");
  });

  it("shows ⌘ K on Apple platforms", async () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    expect(screen.getByRole("button", { name: "Search" })).toHaveTextContent("⌘ K");
  });

  it("has a menu button that shows and hides the same area list on narrow screens", async () => {
    const { user } = renderShell("/catalog");
    await screen.findByRole("heading", { level: 1, name: "Catalog" });

    const toggle = screen.getByRole("button", { name: "Menu" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(panel).not.toBeNull();
    expect(panel).toHaveAttribute("data-open", "false");
    expect(within(panel as HTMLElement).getByRole("navigation")).toBeInTheDocument();
    expect(
      within(panel as HTMLElement).getByRole("button", { name: "Search" }),
    ).toBeInTheDocument();
    expect(
      within(panel as HTMLElement).getByRole("button", { name: "Not signed in" }),
    ).toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel).toHaveAttribute("data-open", "true");

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("closes the narrow menu after choosing an area", async () => {
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    const toggle = screen.getByRole("button", { name: "Menu" });

    await user.click(toggle);
    await user.click(screen.getByRole("link", { name: "Jobs" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("Escape closes the narrow menu and returns focus to its button", async () => {
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    const toggle = screen.getByRole("button", { name: "Menu" });
    await user.click(toggle);
    await user.tab();

    await user.keyboard("{Escape}");

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
  });

  it("Escape does nothing while the narrow menu is closed", async () => {
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    await user.click(screen.getByRole("link", { name: "Catalog" }));
    await screen.findByRole("heading", { level: 1, name: "Catalog" });

    await user.keyboard("{Escape}");

    expect(screen.getByRole("heading", { level: 1, name: "Catalog" })).toHaveFocus();
  });
});
