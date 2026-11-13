import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { GraphOut } from "../../api";
import { expectPending, held } from "../../test/pending";
import { renderShell } from "../../test/render";
import type { FakeServer } from "../../test/server";
import { DOWN, graphNode, LINEAGE_PATH, lineage, TABLE, UP, UP2 } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const TAB = `${PAGE}/lineage`;
const ROUTE = `GET ${LINEAGE_PATH}`;

/**
 * A lineage route that answers each way and depth with its own answer.
 *
 * @param answers - The answer by `up` or `down` and the depth, such as `up2`; the depth-1
 *   answers {@link UP} and {@link DOWN} otherwise.
 * @returns The route.
 */
function answering(
  answers: Readonly<Record<string, GraphOut>>,
): (request: { readonly path: string }) => { status: number; body: GraphOut } {
  return (request) => {
    const query = new URLSearchParams(request.path.split("?")[1]);
    const way = query.get("direction") as string;
    const fallback = way === "up" ? UP : DOWN;
    return { status: 200, body: answers[`${way}${query.get("depth") as string}`] ?? fallback };
  };
}

/**
 * The depths of the lineage the tab asked for, one way.
 *
 * @param server - The fake server.
 * @param way - `up` or `down`.
 * @returns The depths, in order.
 */
function depths(server: FakeServer, way: "up" | "down"): string[] {
  return server.sent
    .filter((request) => request.path.startsWith(LINEAGE_PATH))
    .map((request) => new URLSearchParams(request.path.split("?")[1]))
    .filter((query) => query.get("direction") === way)
    .map((query) => query.get("depth") as string);
}

/**
 * The drawing's region, once the lineage arrived.
 *
 * @returns The region.
 */
function region(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Lineage of fact_sales" });
}

/**
 * The link of a table's box in the drawing.
 *
 * @param path - The table page the link opens.
 * @returns The link.
 */
async function boxLink(path: string): Promise<Element> {
  const drawing = await region();
  const link = drawing.querySelector(`a[href="${path}"]`);
  expect(link).not.toBeNull();
  return link as Element;
}

describe("LineageTab", () => {
  it("opens one step each way from the table's id", async () => {
    const { server } = renderShell(TAB);

    await region();

    const node = `node=table%3A${TABLE.uuid}`;
    const paths = server.sent.map((request) => request.path);
    expect(paths).toContain(`${LINEAGE_PATH}?${node}&direction=up&depth=1&limit=500`);
    expect(paths).toContain(`${LINEAGE_PATH}?${node}&direction=down&depth=1&limit=500`);
  });

  it("draws the lineage in a focusable region beside its text alternative", async () => {
    renderShell(TAB);

    const drawing = await region();

    expect(drawing).toHaveAttribute("tabindex", "0");
    expect(drawing.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("list", { name: "Lineage as a list" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "As a list" })).toBeVisible();
  });

  it("says it reads the lineage until both ways arrived", async () => {
    const slow = held();
    renderShell(TAB, { routes: { [ROUTE]: slow.route } });

    expect(await screen.findByText("Reading the lineage…")).toHaveAttribute("role", "status");

    await act(async () => {
      slow.release({ status: 200, body: UP });
      await Promise.resolve();
    });
    expect(await region()).toBeVisible();
  });

  it("expands one step up and keeps it in the address", async () => {
    const { user, router, server } = renderShell(TAB, {
      routes: { [ROUTE]: answering({ up2: UP2 }) },
    });
    await region();

    await user.click(screen.getByRole("button", { name: "One step up" }));

    expect(router.state.location.search).toBe("?up=2");
    expect(await boxLink("/catalog/prod/bronze/sales_raw")).toBeTruthy();
    // Something is two steps up, so there may be more.
    expect(screen.getByRole("button", { name: "One step up" })).toHaveFocus();
    expect(depths(server, "up")).toEqual(["1", "2"]);

    await act(async () => {
      await router.navigate(-1);
    });

    expect(router.state.location.search).toBe("");
    await waitFor(() => {
      expect(depths(server, "up")).toEqual(["1", "2", "1"]);
    });
    await waitFor(async () => {
      expect((await region()).querySelector('a[href="/catalog/prod/bronze/sales_raw"]')).toBeNull();
    });
  });

  it("waits on the pressed button while the next step is read", async () => {
    const slow = held();
    const { user } = renderShell(TAB, {
      routes: {
        [ROUTE]: (request) =>
          request.path.includes("direction=down&depth=2") ? slow.route() : answering({})(request),
      },
    });
    await region();

    await user.click(screen.getByRole("button", { name: "One step down" }));

    expectPending(screen.getByRole("button", { name: /One step down/ }));
    await act(async () => {
      slow.release({ status: 200, body: DOWN });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /One step down/ })).not.toBeInTheDocument();
    });
  });

  it("hides One step up when no table is at that depth and moves focus to the graph", async () => {
    const { user } = renderShell(TAB, { routes: { [ROUTE]: answering({ up2: UP }) } });
    await region();

    await user.click(screen.getByRole("button", { name: "One step up" }));

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /One step up/ })).not.toBeInTheDocument();
    });
    expect(await region()).toHaveFocus();
    expect(screen.getByRole("button", { name: "One step down" })).toBeVisible();
  });

  it("hides One step down the same way", async () => {
    const { user, router } = renderShell(TAB, { routes: { [ROUTE]: answering({ down2: DOWN }) } });
    await region();

    await user.click(screen.getByRole("button", { name: "One step down" }));

    expect(router.state.location.search).toBe("?down=2");
    await waitFor(async () => {
      expect(await region()).toHaveFocus();
    });
    expect(screen.queryByRole("button", { name: /One step down/ })).not.toBeInTheDocument();
  });

  it("offers no step up when nothing is upstream", async () => {
    renderShell(TAB, { routes: { [ROUTE]: answering({ up1: lineage([]) }) } });
    await region();

    expect(screen.queryByRole("button", { name: "One step up" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "One step down" })).toBeVisible();
  });

  it("reads bad depths in the address as one step and caps at ten", async () => {
    const far = lineage([graphNode("table", "gold.far_away", 10)]);
    const { server } = renderShell(`${TAB}?up=abc&down=12`, {
      routes: { [ROUTE]: answering({ down10: far }) },
    });

    await region();

    expect(depths(server, "up")).toEqual(["1"]);
    expect(depths(server, "down")).toEqual(["10"]);
    // Ten steps is the most the server walks, so there is no further step.
    expect(screen.queryByRole("button", { name: "One step down" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "One step up" })).toBeVisible();
  });

  it("opens the full-screen view and returns focus to its button", async () => {
    const { user } = renderShell(TAB);
    await region();
    const open = screen.getByRole("button", { name: "Full screen" });

    await user.click(open);

    const dialog = await screen.findByRole("dialog", { name: "Lineage of fact_sales" });
    expect(dialog.querySelector("svg")).not.toBeNull();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(open).toHaveFocus();
    });

    await user.click(open);
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Close" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(open).toHaveFocus();
    });
  });

  it("opens a table from the drawing without loading the page again", async () => {
    const { router } = renderShell(TAB);
    const path = "/catalog/prod/staging/stg_sales";

    // A click with a modifier is the browser's: a new tab or window.
    for (const modifier of ["ctrlKey", "metaKey", "shiftKey", "altKey"]) {
      expect(fireEvent.click(await boxLink(path), { [modifier]: true })).toBe(true);
    }
    expect(fireEvent.click(await boxLink(path), { button: 1 })).toBe(true);
    expect(router.state.location.pathname).toBe(TAB);

    expect(fireEvent.click(await boxLink(path))).toBe(false);

    expect(router.state.location.pathname).toBe(path);
  });

  it.each(["Enter", " "])("opens a table from the drawing with %j on its link", async (key) => {
    const { router } = renderShell(TAB);
    const link = (await boxLink("/catalog/prod/staging/stg_sales")) as SVGAElement;

    expect(fireEvent.keyDown(link, { key: "Tab" })).toBe(true);
    expect(router.state.location.pathname).toBe(TAB);
    expect(fireEvent.keyDown(link, { key })).toBe(false);

    expect(router.state.location.pathname).toBe("/catalog/prod/staging/stg_sales");
  });

  it("shows no hidden count", async () => {
    renderShell(TAB);
    await region();

    const tab = screen.getByRole("region", { name: "Lineage" });
    expect(tab.textContent).not.toMatch(/hidden|not shown|more/i);
  });

  it("says nothing is there for a reader who sees no builder", async () => {
    renderShell(TAB, { routes: { [ROUTE]: { status: 200, body: lineage([]) } } });

    expect(await screen.findByText("Nothing upstream or downstream.")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Lineage of fact_sales" })).toBeNull();
    expect(
      within(screen.getByRole("region", { name: "Lineage" })).queryByRole("button"),
    ).toBeNull();
  });

  it("says when only the first 500 nodes are shown", async () => {
    renderShell(TAB, {
      routes: { [ROUTE]: answering({ up1: { ...UP, next_cursor: "next" } }) },
    });
    await region();

    expect(screen.getByText("Showing the first 500 nodes.")).toBeVisible();
  });

  it("names the cause and offers Try again when lineage cannot be read", async () => {
    let failing = true;
    const { user } = renderShell(TAB, {
      routes: {
        [ROUTE]: (request) =>
          failing
            ? { status: 503, body: { detail: "The graph is rebuilding." } }
            : answering({})(request),
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The graph is rebuilding.");
    failing = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));

    const drawing = await region();
    expect(drawing.parentElement).toHaveFocus();
  });
});
