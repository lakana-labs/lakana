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
});
