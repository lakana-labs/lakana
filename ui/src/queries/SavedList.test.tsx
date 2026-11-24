import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { aListedSavedQuery } from "../test/queries";
import { fakeServer, type FakeRoute } from "../test/server";
import { SavedList } from "./SavedList";

const SAVED = "GET /api/v1/saved-queries";

/**
 * Renders the Saved list of `kim.lee`.
 *
 * @param filter - The environment the list is filtered by, or null.
 * @param reloadKey - The key that reads the list again when it changes.
 * @returns What `render` hands back.
 */
function renderSaved(filter: string | null = null, reloadKey = 0): ReturnType<typeof render> {
  return render(
    <MemoryRouter>
      <SavedList me="kim.lee" filter={filter} reloadKey={reloadKey} />
    </MemoryRouter>,
  );
}

/**
 * A route that answers the list's pages by cursor.
 *
 * @param pages - The pages by cursor; "" for the first.
 * @returns The route.
 */
function byCursor(
  pages: Record<string, { items: unknown[]; next_cursor: string | null }>,
): FakeRoute {
  return (request) => {
    const cursor = new URL(request.path, "http://localhost").searchParams.get("cursor") ?? "";
    return { status: 200, body: pages[cursor] };
  };
}

describe("SavedList", () => {
  it("lists names as links with everyone, owner or former owner, and the environment label", async () => {
    fakeServer({
      [SAVED]: {
        status: 200,
        body: {
          items: [
            aListedSavedQuery(),
            aListedSavedQuery({ id: "b2", name: "Station ranking", visibility: "everyone" }),
            aListedSavedQuery({
              id: "c3",
              name: "Late trains",
              environment: "dev",
              owner: "sam.ortiz",
              visibility: "everyone",
              actions: [],
            }),
            aListedSavedQuery({
              id: "d4",
              name: "Old \u202Eview",
              owner: null,
              former_owner: true,
            }),
          ],
          next_cursor: null,
        },
      },
    });
    renderSaved();

    const region = await screen.findByRole("region", { name: "Saved queries" });
    const rows = within(region).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]?.textContent)).toEqual([
      "Daily sales Environment prod",
      "Station ranking Environment prodeveryone",
      "Late trains Environment deveveryone · by sam.ortiz",
      "Old U+202Eview Environment prodformer owner",
    ]);
    expect(rows.every((row) => row.textContent.endsWith("›"))).toBe(true);
    const link = within(region).getByRole("link", { name: "Late trains" });
    expect(link).toHaveAttribute("href", "/queries/dev/saved/c3");
    expect(link).toHaveClass("row-name");
    expect(region.querySelector("table")).not.toHaveClass("list-static");
    expect(region).toHaveAttribute("tabindex", "0");
  });

  it("shows more by cursor", async () => {
    const server = fakeServer({
      [SAVED]: byCursor({
        "": { items: [aListedSavedQuery()], next_cursor: "c-2" },
        "c-2": {
          items: [aListedSavedQuery({ id: "b2", name: "Station ranking" })],
          next_cursor: null,
        },
      }),
    });
    const user = userEvent.setup();
    renderSaved("prod");

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    const next = await screen.findByRole("link", { name: "Station ranking" });
    await waitFor(() => {
      expect(next).toHaveFocus();
    });
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/saved-queries?limit=50&environment=prod",
      "/api/v1/saved-queries?limit=50&environment=prod&cursor=c-2",
    ]);
  });

  it("says when there is none", async () => {
    fakeServer({ [SAVED]: { status: 200, body: { items: [], next_cursor: null } } });
    renderSaved();

    expect(
      await screen.findByText("No saved queries yet. Save… keeps the query in the editor."),
    ).toBeInTheDocument();
  });

  it("reads the list again when the reload key changes, keeping the rows meanwhile", async () => {
    let answer = [aListedSavedQuery()];
    const server = fakeServer({
      [SAVED]: () => ({ status: 200, body: { items: answer, next_cursor: null } }),
    });
    const { rerender } = renderSaved();
    await screen.findByRole("link", { name: "Daily sales" });
    answer = [aListedSavedQuery(), aListedSavedQuery({ id: "b2", name: "Station ranking" })];

    rerender(
      <MemoryRouter>
        <SavedList me="kim.lee" filter={null} reloadKey={1} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Daily sales" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Station ranking" })).toBeInTheDocument();
    expect(server.sent).toHaveLength(2);
  });

  it("says why the list could not be read", async () => {
    fakeServer({
      [SAVED]: { status: 503, body: { detail: "The server is busy; try again.", type: "Busy" } },
    });
    renderSaved();

    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy; try again.");
  });
});

describe("SavedList's sort", () => {
  it("shows when each changed, and sorts by Changed or Name on the server", async () => {
    const server = fakeServer({
      [SAVED]: { status: 200, body: { items: [aListedSavedQuery()], next_cursor: null } },
    });
    renderSaved();
    const name = await screen.findByRole("columnheader", { name: /Name/u });
    expect(name).toHaveAttribute("aria-sort", "ascending");
    const row = screen.getAllByRole("row")[1] as HTMLElement;
    expect(within(row).getByRole("time")).toHaveAttribute(
      "datetime",
      aListedSavedQuery().updated_at,
    );
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /Changed/u }));
    await waitFor(() => {
      expect(server.sent.at(-1)?.path).toContain("sort=updated&order=desc");
    });
    await user.click(screen.getByRole("button", { name: /^Name/u }));
    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /Name/u })).toHaveAttribute(
        "aria-sort",
        "ascending",
      );
    });
    // Back at the list's own order: the request names no sort.
    expect(server.sent.at(-1)?.path).not.toContain("sort=");
  });
});
