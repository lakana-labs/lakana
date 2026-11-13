import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

let field: HTMLInputElement | null = null;

afterEach(() => {
  field?.remove();
  field = null;
});

/**
 * Renders the shell at Home and waits for it.
 *
 * @param options - Passed on to {@link renderShell}.
 * @returns The shell.
 */
async function shellAtHome(
  options: Parameters<typeof renderShell>[1] = {},
): Promise<ReturnType<typeof renderShell>> {
  const shell = renderShell("/", options);
  await screen.findByRole("heading", { level: 1, name: "Home" });
  return shell;
}

describe("CommandPalette", () => {
  it.each([
    ["Control", { ctrlKey: true }],
    ["Meta", { metaKey: true }],
  ] as const)("%s+K opens the palette, even from a text field", async (_name, modifier) => {
    await shellAtHome();
    field = document.createElement("input");
    field.setAttribute("aria-label", "A text field");
    document.body.append(field);
    field.focus();

    const notPrevented = fireEvent.keyDown(field, { key: "k", ...modifier });

    expect(notPrevented).toBe(false);
    expect(await screen.findByRole("dialog", { name: "Search" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox")).toHaveFocus();
  });

  it("ignores K without a modifier, with Shift or Alt, and other keys", async () => {
    await shellAtHome();

    expect(fireEvent.keyDown(document.body, { key: "k" })).toBe(true);
    expect(fireEvent.keyDown(document.body, { key: "j", ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(document.body, { key: "K", ctrlKey: true, shiftKey: true })).toBe(
      true,
    );
    expect(fireEvent.keyDown(document.body, { key: "k", ctrlKey: true, altKey: true })).toBe(true);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens with a capital K too (Caps Lock)", async () => {
    await shellAtHome();

    fireEvent.keyDown(document.body, { key: "K", ctrlKey: true });

    expect(await screen.findByRole("dialog", { name: "Search" })).toBeInTheDocument();
  });

  it("lists one Go-to command per visible area, then Help's commands", async () => {
    const { user } = await shellAtHome({ access: new Set(["home", "catalog", "access"]) });

    await user.keyboard("{Control>}k{/Control}");

    const menu = await screen.findByRole("menu");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual([
      "Go to Home",
      "Go to Catalog",
      "Go to Access",
      "Take the tour again",
      "Keyboard shortcuts",
    ]);
    expect(screen.getByRole("searchbox")).toHaveAttribute(
      "placeholder",
      "Go to a place or run a command",
    );
  });

  it("filters as you type and says when nothing matches", async () => {
    const { user } = await shellAtHome();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("menu");

    await user.keyboard("conn");
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Go to Connections",
    ]);

    await user.keyboard("zz");
    expect(screen.queryAllByRole("menuitem", { name: /^Go to/ })).toEqual([]);
    expect(
      within(screen.getByRole("menu", { name: "Places and commands" })).getByText(
        "Nothing matches “connzz”.",
      ),
    ).toBeInTheDocument();
  });

  it("announces how many commands match, or that none does", async () => {
    const { user } = await shellAtHome();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Search" });
    const status = within(dialog).getByRole("status");
    expect(status).toHaveTextContent(/^$/);
    expect(status).toHaveClass("visually-hidden");

    await user.keyboard("go");
    expect(status).toHaveTextContent("7 results");
    await user.keyboard("{Backspace}{Backspace}conn");
    expect(status).toHaveTextContent("1 result");
    await user.keyboard("zz");
    expect(within(dialog).getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("Nothing matches “connzz”.");
  });

  it("names the list of commands apart from the dialog and the field", async () => {
    const { user } = await shellAtHome();
    await user.keyboard("{Control>}k{/Control}");

    expect(await screen.findByRole("menu", { name: "Places and commands" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search" })).toBeInTheDocument();
  });

  it("opens the Catalog at the last choice, like the menu's link", async () => {
    window.localStorage.setItem(
      "lakana.catalog.admin",
      JSON.stringify({ environment: "prod", namespace: "gold" }),
    );
    const { user, router } = renderShell("/catalog?env=staging");
    await screen.findByText(/There is no environment named staging/);

    await user.keyboard("{Control>}k{/Control}");
    await user.click(await screen.findByRole("menuitem", { name: "Go to Catalog" }));

    await waitFor(() => {
      expect(router.state.location.search).toBe("?env=prod&ns=gold");
    });
  });

  it("Enter goes to the area and closes; Escape closes and returns focus", async () => {
    const { user, router } = await shellAtHome();
    const search = screen.getByRole("button", { name: "Search" });

    await user.click(search);
    await screen.findByRole("dialog", { name: "Search" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(search).toHaveFocus();
    });

    await user.click(search);
    await screen.findByRole("dialog", { name: "Search" });
    await user.keyboard("jobs");
    await user.keyboard("{ArrowDown}{Enter}");

    expect(router.state.location.pathname).toBe("/jobs");
    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toHaveFocus();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("the Search button opens the same palette", async () => {
    const { user } = await shellAtHome();

    await user.click(screen.getByRole("button", { name: "Search" }));

    const dialog = await screen.findByRole("dialog", { name: "Search" });
    expect(within(dialog).getAllByRole("menuitem")).toHaveLength(9);
  });
});
