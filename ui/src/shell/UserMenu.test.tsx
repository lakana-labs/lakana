import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PREFERENCES_KEY } from "../preferences/preferences";
import { renderShell } from "../test/render";

const root = document.documentElement;

afterEach(() => {
  root.removeAttribute("data-theme");
  root.removeAttribute("data-density");
});

/**
 * Reads the stored preferences.
 *
 * @param storage - The storage the shell was given.
 * @returns The parsed value.
 */
function stored(storage: Storage): unknown {
  return JSON.parse(storage.getItem(PREFERENCES_KEY) ?? "null");
}

/**
 * Renders the shell and opens the user menu.
 *
 * @returns The shell.
 */
async function openMenu(): Promise<ReturnType<typeof renderShell>> {
  const shell = renderShell("/");
  await screen.findByRole("heading", { level: 1, name: "Home" });
  await shell.user.click(screen.getByRole("button", { name: "Not signed in" }));
  await screen.findByRole("menu", { name: "Preferences" });
  return shell;
}

describe("UserMenu", () => {
  it("switches the theme and remembers it", async () => {
    const { user, storage } = await openMenu();
    expect(screen.getByRole("menuitemradio", { name: "System" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.click(screen.getByRole("menuitemradio", { name: "Dark" }));

    expect(root.getAttribute("data-theme")).toBe("dark");
    expect(stored(storage)).toEqual({ theme: "dark", density: "roomy", language: "en" });
    await user.click(screen.getByRole("button", { name: "Not signed in" }));
    expect(await screen.findByRole("menuitemradio", { name: "Dark" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.click(screen.getByRole("menuitemradio", { name: "System" }));
    expect(root.hasAttribute("data-theme")).toBe(false);
  });

  it("switches the density and remembers it", async () => {
    const { user, storage } = await openMenu();

    await user.click(screen.getByRole("menuitemradio", { name: "Compact" }));

    expect(root.getAttribute("data-density")).toBe("compact");
    expect(stored(storage)).toEqual({ theme: "system", density: "compact", language: "en" });
  });

  it("offers English as the only language", async () => {
    const { user } = await openMenu();

    const language = screen.getByRole("group", { name: "Language" });
    const options = within(language).getAllByRole("menuitemradio");
    expect(options.map((option) => option.textContent)).toEqual(["English"]);
    expect(options[0]).toHaveAttribute("aria-checked", "true");

    await user.click(screen.getByRole("menuitemradio", { name: "English" }));
    expect(root.lang).toBe("en");
  });

  it("starts from stored preferences", async () => {
    const shell = renderShell("/", {
      storage: storageWith('{"theme":"light","density":"compact"}'),
    });
    await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(root.getAttribute("data-theme")).toBe("light");

    await shell.user.click(screen.getByRole("button", { name: "Not signed in" }));

    expect(await screen.findByRole("menuitemradio", { name: "Light" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("menuitemradio", { name: "Compact" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("shows Not signed in and the preview line", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    expect(screen.getByRole("button", { name: "Not signed in" })).toBeInTheDocument();
    expect(
      screen.getByText("Preview: sign-in comes later, and nothing here reads your data yet."),
    ).toBeInTheDocument();
  });
});

/**
 * A storage holding one stored preferences value.
 *
 * @param value - The raw stored text.
 * @returns The storage.
 */
function storageWith(value: string): Storage {
  const storage = window.sessionStorage;
  storage.clear();
  storage.setItem(PREFERENCES_KEY, value);
  return storage;
}
