import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PREFERENCES_KEY } from "../preferences/preferences";
import { ApiError } from "../api";
import { renderShell } from "../test/render";
import { ADMIN_SESSION } from "../test/server";
import { signedIn } from "../test/session";

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
 * Renders the shell with a session and opens the user menu.
 *
 * @param session - The session value.
 * @returns The shell.
 */
async function openMenuWith(
  session: ReturnType<typeof signedIn>,
): Promise<ReturnType<typeof renderShell>> {
  const shell = renderShell("/", { session });
  await screen.findByRole("heading", { level: 1, name: "Home" });
  await shell.user.click(screen.getByRole("button", { name: "Signed in as admin" }));
  await screen.findByRole("menu", { name: "Preferences and account" });
  return shell;
}

/**
 * Renders the shell and opens the user menu.
 *
 * @returns The shell.
 */
async function openMenu(): Promise<ReturnType<typeof renderShell>> {
  const shell = renderShell("/");
  await screen.findByRole("heading", { level: 1, name: "Home" });
  await shell.user.click(screen.getByRole("button", { name: "Signed in as admin" }));
  await screen.findByRole("menu", { name: "Preferences and account" });
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
    await user.click(screen.getByRole("button", { name: "Signed in as admin" }));
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

    await shell.user.click(screen.getByRole("button", { name: "Signed in as admin" }));

    expect(await screen.findByRole("menuitemradio", { name: "Light" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("menuitemradio", { name: "Compact" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("shows the signed-in name in the user menu", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    const button = screen.getByRole("button", { name: "Signed in as admin" });
    expect(button).toHaveTextContent("admin");
    expect(screen.queryByText(/Preview/)).not.toBeInTheDocument();
  });

  it("shows the display name when the person has one", async () => {
    renderShell("/", {
      session: signedIn({ session: { ...ADMIN_SESSION, display_name: "Kim Lee" } }),
    });

    expect(await screen.findByRole("button", { name: "Signed in as Kim Lee" })).toBeInTheDocument();
  });

  it("signs out, here or everywhere", async () => {
    const session = signedIn();
    const { user } = await openMenuWith(session);

    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(session.signOut).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Signed in as admin" }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out everywhere" }));
    expect(session.signOutEverywhere).toHaveBeenCalledTimes(1);
  });

  it("says why signing out did not work", async () => {
    const session = signedIn({
      signOut: vi.fn(() =>
        Promise.reject(new ApiError(503, "Unavailable", "The server is busy; try again.")),
      ),
    });
    const { user } = await openMenuWith(session);

    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Signing out did not work: The server is busy; try again.",
    );
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
