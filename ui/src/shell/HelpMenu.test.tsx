import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { STATE } from "../test/home";
import { renderShell } from "../test/render";

const STATE_PATH = "/api/v1/principals/admin/interface-state";

/**
 * Renders the shell and opens Help.
 *
 * @param options - Passed on to {@link renderShell}.
 * @returns The shell and the open menu.
 */
async function openHelp(
  options: Parameters<typeof renderShell>[1] = {},
): Promise<ReturnType<typeof renderShell> & { menu: HTMLElement }> {
  const shell = renderShell("/catalog", options);
  await screen.findByRole("heading", { level: 1, name: "Catalog" });
  await shell.user.click(screen.getByRole("button", { name: "Help" }));
  const menu = await screen.findByRole("menu", { name: "Help" });
  return { ...shell, menu };
}

describe("HelpMenu", () => {
  it("sits after the areas and before the user menu", async () => {
    renderShell("/");
    await screen.findByRole("heading", { level: 1, name: "Home" });

    const banner = screen.getByRole("banner");
    const order = within(banner)
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label") ?? button.textContent);
    expect(order.slice(-2)).toEqual(["Help", "Signed in as admin"]);
    const settings = within(banner).getByRole("link", { name: "Settings" });
    const help = within(banner).getByRole("button", { name: "Help" });
    expect(settings.compareDocumentPosition(help) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("lists its items, the documentation opening in a new tab", async () => {
    const { menu } = await openHelp();

    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual([
      "Take the tour again",
      "Getting started (opens in a new tab)",
      "How-to guides (opens in a new tab)",
      "Documentation (opens in a new tab)",
      "Keyboard shortcuts",
    ]);
    for (const [name, path] of [
      ["Getting started", "tutorials/getting-started/"],
      ["How-to guides", "how-to/"],
      ["Documentation", ""],
    ] as const) {
      // jsdom drops the space before the hidden "(opens in a new tab)"; browsers keep it.
      const item = within(menu).getByRole("menuitem", { name: new RegExp(`^${name}`) });
      expect(item).toHaveAttribute("href", `https://lakana-labs.github.io/lakana/${path}`);
      expect(item).toHaveAttribute("target", "_blank");
      expect(item).toHaveAttribute("rel", "noreferrer");
    }
  });

  it("shows the version number in Help", async () => {
    await openHelp();

    expect(await screen.findByText("Lakana 0.23.0")).toBeInTheDocument();
  });

  it("shows no version while the server's is unknown", async () => {
    await openHelp({ routes: { "GET /api/v1/info": { status: 503, body: { detail: "down" } } } });

    expect(screen.queryByText(/^Lakana \d/)).not.toBeInTheDocument();
  });

  it("offers Show Get started again only when hidden", async () => {
    const { menu } = await openHelp();

    expect(
      within(menu).queryByRole("menuitem", { name: "Show “Get started” again" }),
    ).not.toBeInTheDocument();
  });

  it("brings Get started back and goes Home", async () => {
    let stored = { ...STATE, get_started_hidden: true };
    const { user, router, server, menu } = await openHelp({
      routes: {
        [`GET ${STATE_PATH}`]: () => ({ status: 200, body: stored }),
        [`PATCH ${STATE_PATH}`]: (request) => {
          stored = { ...stored, ...(request.body as object) };
          return { status: 200, body: stored };
        },
      },
    });

    await user.click(
      await within(menu).findByRole("menuitem", { name: "Show “Get started” again" }),
    );

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/");
    });
    expect(
      server.sent.filter((request) => request.method === "PATCH").map((request) => request.body),
    ).toEqual([{ get_started_hidden: false }]);
  });

  it("says so when Get started cannot be brought back", async () => {
    const { user, router, menu } = await openHelp({
      routes: {
        [`GET ${STATE_PATH}`]: { status: 200, body: { ...STATE, get_started_hidden: true } },
        [`PATCH ${STATE_PATH}`]: { status: 503, body: { detail: "The server is busy." } },
      },
    });

    await user.click(
      await within(menu).findByRole("menuitem", { name: "Show “Get started” again" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /^Showing Get started again did not work: /,
    );
    expect(router.state.location.pathname).toBe("/catalog");
  });

  it("opens the keyboard shortcuts", async () => {
    const { user, menu } = await openHelp();

    await user.click(within(menu).getByRole("menuitem", { name: "Keyboard shortcuts" }));

    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  });
});
