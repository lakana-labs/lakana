import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderShell } from "../test/render";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ShortcutsDialog", () => {
  it("lists the shortcuts in a dialog", async () => {
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    await user.keyboard("{Control>}k{/Control}");
    await user.keyboard("shortcuts");
    await user.keyboard("{ArrowDown}{Enter}");

    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    const terms = within(dialog)
      .getAllByRole("term")
      .map((term) => term.textContent);
    expect(terms).toEqual(["Ctrl K", "Esc", "Tab, Shift Tab", "Enter"]);
    const details = within(dialog)
      .getAllByRole("definition")
      .map((detail) => detail.textContent);
    expect(details).toEqual([
      "Open search: go to a place or run a command.",
      "Close a menu, a dialog or the tour.",
      "Move to the next or the previous control.",
      "Open the row that has focus.",
    ]);
  });

  it("closes with its button and returns focus to the page's heading", async () => {
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    await user.click(screen.getByRole("button", { name: "Help" }));
    await user.click(await screen.findByRole("menuitem", { name: "Keyboard shortcuts" }));
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });

    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Help" })).toHaveFocus();
    });
  });

  it("closes with Escape", async () => {
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    await user.click(screen.getByRole("button", { name: "Help" }));
    await user.click(await screen.findByRole("menuitem", { name: "Keyboard shortcuts" }));
    await screen.findByRole("dialog", { name: "Keyboard shortcuts" });

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("names ⌘ K on a Mac", async () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    const { user } = renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    await user.click(screen.getByRole("button", { name: "Help" }));
    await user.click(await screen.findByRole("menuitem", { name: "Keyboard shortcuts" }));

    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(dialog).getAllByRole("term")[0]).toHaveTextContent("⌘ K");
  });
});
