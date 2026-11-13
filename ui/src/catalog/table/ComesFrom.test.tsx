import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../../test/render";
import { DOWN, LINEAGE_PATH, lineage, ROOT, TABLE, UP } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";

/**
 * The section, once the page has arrived.
 *
 * @returns The section's element.
 */
async function section(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 2, name: "Comes from · goes to" });
  return heading.parentElement as HTMLElement;
}

/**
 * Tables as nodes at distance 1.
 *
 * @param names - The dotted names.
 * @returns The nodes.
 */
function tables(names: readonly string[]): (typeof ROOT)[] {
  return names.map((name) => ({ id: `table:${name}`, kind: "table", name, distance: 1 }));
}

describe("ComesFrom", () => {
  it("starts lineage at the table's id, one step each way", async () => {
    const { server } = renderShell(PAGE);

    await section();

    const node = `node=table%3A${TABLE.uuid}`;
    await waitFor(() => {
      const paths = server.sent
        .map((request) => request.path)
        .filter((path) => path.startsWith(LINEAGE_PATH));
      expect(paths).toContain(`${LINEAGE_PATH}?${node}&direction=up&depth=1&limit=500`);
      expect(paths).toContain(`${LINEAGE_PATH}?${node}&direction=down&depth=1&limit=500`);
    });
  });

  it("links the tables directly upstream and downstream, not jobs or connections", async () => {
    renderShell(PAGE);

    const box = await section();

    const up = await within(box).findByRole("link", { name: "staging.stg_sales" });
    expect(up).toHaveAttribute("href", "/catalog/prod/staging/stg_sales");
    expect(within(box).getByRole("link", { name: "gold.sales.daily_sales" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold.sales/daily_sales",
    );
    expect(within(box).queryByText(/load_sales|shop_db/)).not.toBeInTheDocument();
    expect(within(box).getAllByRole("link")).toHaveLength(2);
  });

  it("shows at most five each way and counts the rest", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${LINEAGE_PATH}`]: (request) => ({
          status: 200,
          body: request.path.includes("direction=up")
            ? lineage(tables(["a.t1", "a.t2", "a.t3", "a.t4", "a.t5", "a.t6", "a.t7"]))
            : DOWN,
        }),
      },
    });

    const box = await section();

    expect(await within(box).findByText("and 2 more")).toBeVisible();
    expect(within(box).getAllByRole("link")).toHaveLength(6);
  });

  it("names a table whose reference cannot be split in plain text, with no link", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${LINEAGE_PATH}`]: (request) => ({
          status: 200,
          body: request.path.includes("direction=up") ? lineage(tables(["nodot"])) : DOWN,
        }),
      },
    });

    const box = await section();

    expect(await within(box).findByText("nodot")).toBeVisible();
    expect(within(box).queryByRole("link", { name: "nodot" })).not.toBeInTheDocument();
  });

  it("says Nothing upstream and Nothing downstream", async () => {
    renderShell(PAGE, {
      routes: { [`GET ${LINEAGE_PATH}`]: { status: 200, body: lineage([]) } },
    });

    const box = await section();

    expect(await within(box).findByText("Nothing upstream.")).toBeVisible();
    expect(within(box).getByText("Nothing downstream.")).toBeVisible();
  });

  it("names the cause and offers Try again when the lineage cannot be read, with the rest of Overview in place", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${LINEAGE_PATH}`]: { status: 503, body: { detail: "The graph is rebuilding." } },
      },
    });

    const box = await section();

    expect(await within(box).findByRole("alert")).toHaveTextContent("The graph is rebuilding.");
    expect(within(box).getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.getByText("Every sale in every store, one row per sale.")).toBeVisible();
  });

  it("moves focus to the section heading after Try again succeeds", async () => {
    let failing = true;
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${LINEAGE_PATH}`]: (request) =>
          failing
            ? { status: 503, body: { detail: "The graph is rebuilding." } }
            : { status: 200, body: request.path.includes("direction=up") ? UP : DOWN },
      },
    });
    const box = await section();
    const retry = await within(box).findByRole("button", { name: "Try again" });
    failing = false;

    await user.click(retry);

    expect(await within(box).findByRole("link", { name: "staging.stg_sales" })).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "Comes from · goes to" })).toHaveFocus();
  });

  it("reads only the lineage that failed again", async () => {
    let upCalls = 0;
    const { user, server } = renderShell(PAGE, {
      routes: {
        [`GET ${LINEAGE_PATH}`]: (request) => {
          if (request.path.includes("direction=up")) {
            upCalls += 1;
            return upCalls === 1
              ? { status: 503, body: { detail: "The graph is rebuilding." } }
              : { status: 200, body: UP };
          }
          return { status: 200, body: DOWN };
        },
      },
    });
    const box = await section();

    await user.click(await within(box).findByRole("button", { name: "Try again" }));

    await within(box).findByRole("link", { name: "staging.stg_sales" });
    const down = server.sent.filter((request) => request.path.includes("direction=down"));
    expect(down).toHaveLength(1);
  });
});
