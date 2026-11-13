import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { TableMatchOut } from "../api";
import { item, PAGE, TREE } from "../test/catalog";
import { held } from "../test/pending";
import { renderShell } from "../test/render";

const EMPTY = { items: [], next_cursor: null, capped: false };

/**
 * The list's rows below the head.
 *
 * @returns The rows.
 */
function rows(): HTMLElement[] {
  return within(screen.getByRole("region", { name: "Tables" }))
    .getAllByRole("row")
    .slice(1);
}

describe("CatalogList", () => {
  it("lists the tables under the Catalog heading with the count", async () => {
    renderShell("/catalog");

    expect(await screen.findByRole("heading", { level: 1, name: "Catalog" })).toBeInTheDocument();
    await screen.findByRole("link", { name: "fact_sales" });
    expect(rows()).toHaveLength(3);
    expect(screen.getByRole("status")).toHaveTextContent("3 tables");
  });

  it("sorts by name ascending by default", async () => {
    const { server } = renderShell("/catalog");

    await screen.findByRole("link", { name: "fact_sales" });

    expect(server.sent.find((r) => r.path.startsWith("/api/v1/tables?"))?.path).toContain(
      "sort=name&order=asc",
    );
    expect(screen.getByRole("columnheader", { name: /Name/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });

  it("sorts by a header and flips it on a second press", async () => {
    const { user, router, server } = renderShell("/catalog");
    await screen.findByRole("link", { name: "fact_sales" });

    await user.click(screen.getByRole("button", { name: "Rows" }));

    expect(router.state.location.search).toBe("?sort=rows");
    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /Rows/ })).toHaveAttribute(
        "aria-sort",
        "descending",
      );
    });
    await user.click(screen.getByRole("button", { name: "Rows" }));

    expect(router.state.location.search).toBe("?sort=rows&order=asc");
    await waitFor(() => {
      expect(screen.getByRole("columnheader", { name: /Rows/ })).toHaveAttribute(
        "aria-sort",
        "ascending",
      );
    });
    expect(server.sent.at(-1)?.path).toContain("sort=rows&order=asc");
  });

  it("offers Created in Sort by, with the order in words", async () => {
    const { user, router } = renderShell("/catalog");
    await screen.findByRole("link", { name: "fact_sales" });

    await user.click(screen.getByRole("button", { name: /Sort by/ }));
    await user.click(screen.getByRole("option", { name: "Created" }));

    expect(router.state.location.search).toBe("?sort=created");
    expect(screen.getByRole("button", { name: /Order/ })).toHaveTextContent("Newest first");
  });

  it("starts from the view in the address", async () => {
    const { server } = renderShell("/catalog?env=prod&ns=gold&sort=rows");

    await screen.findByRole("link", { name: "fact_sales" });

    const path = server.sent.find((r) => r.path.startsWith("/api/v1/tables?"))?.path;
    expect(path).toContain("environment=prod");
    expect(path).toContain("namespace=gold");
    expect(path).toContain("sort=rows&order=desc");
  });

  it("opens the table page from a row", async () => {
    const { user, router } = renderShell("/catalog");

    await user.click(await screen.findByRole("link", { name: "fact_sales" }));

    expect(router.state.location.pathname).toBe("/catalog/prod/gold.sales/fact_sales");
    expect(await screen.findByRole("heading", { name: "This page does not exist" })).toBeVisible();
  });

  describe("a row opens from its hover-text cells too", () => {
    const TABLE = "/catalog/prod/gold.sales/fact_sales";

    /**
     * The elements of the fact_sales row that carry hover text: rows, size, both times and the
     * health text.
     *
     * @returns The elements.
     */
    async function titled(): Promise<HTMLElement[]> {
      const row = (await screen.findByRole("link", { name: "fact_sales" })).closest("tr");
      return [...(row as HTMLElement).querySelectorAll<HTMLElement>("[title]")];
    }

    it("opens the table page from a plain click on each of them", async () => {
      const { user, router } = renderShell("/catalog");
      const cells = await titled();
      expect(cells.length).toBeGreaterThanOrEqual(5);

      for (let index = 0; index < cells.length; index += 1) {
        await router.navigate("/catalog");
        const fresh = await titled();
        expect(fresh[index]).toHaveAttribute("title");
        await user.click(fresh[index] as HTMLElement);

        expect(router.state.location.pathname).toBe(TABLE);
      }
    });

    it("keeps the hover text", async () => {
      renderShell("/catalog");

      const cells = await titled();

      expect(cells.map((cell) => cell.getAttribute("title"))).not.toContain("");
      expect(cells.some((cell) => /bytes/.test(cell.getAttribute("title") ?? ""))).toBe(true);
    });

    it("leaves a click with a modifier key to the browser", async () => {
      const { user, router } = renderShell("/catalog");
      const [cell] = await titled();

      await user.keyboard("{Control>}");
      await user.click(cell as HTMLElement);
      await user.keyboard("{/Control}");
      await user.keyboard("{Shift>}");
      await user.click(cell as HTMLElement);
      await user.keyboard("{/Shift}");

      expect(router.state.location.pathname).toBe("/catalog");
    });

    it("leaves a middle click alone", async () => {
      const { user, router } = renderShell("/catalog");
      const [cell] = await titled();

      await user.pointer({ keys: "[MouseMiddle]", target: cell as HTMLElement });

      expect(router.state.location.pathname).toBe("/catalog");
    });

    it("navigates once, not twice, for a click on the link", async () => {
      const { user, router } = renderShell("/catalog");
      const link = await screen.findByRole("link", { name: "fact_sales" });
      const navigate = vi.spyOn(router, "navigate");

      await user.click(link);

      expect(router.state.location.pathname).toBe(TABLE);
      expect(navigate).toHaveBeenCalledTimes(1);
    });

    it("navigates once for a click on a hover-text cell", async () => {
      const { user, router } = renderShell("/catalog");
      const [cell] = await titled();
      const navigate = vi.spyOn(router, "navigate");

      await user.click(cell as HTMLElement);

      expect(navigate).toHaveBeenCalledTimes(1);
    });

    it("does not open the table when the click ends a text selection", async () => {
      const { router } = renderShell("/catalog");
      const [cell] = await titled();
      window.getSelection()?.selectAllChildren(cell as HTMLElement);
      expect(window.getSelection()?.isCollapsed).toBe(false);

      fireEvent.click(cell as HTMLElement);

      expect(router.state.location.pathname).toBe("/catalog");
      window.getSelection()?.removeAllRanges();
      fireEvent.click(cell as HTMLElement);
      expect(router.state.location.pathname).toBe(TABLE);
    });
  });

  it("shows skeleton rows first and the words after a second", async () => {
    const slow = held();
    renderShell("/catalog", { routes: { "GET /api/v1/tables": slow.route } });

    await screen.findByRole("heading", { level: 1, name: "Catalog" });
    expect(screen.queryByText("Loading tables…")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".skeleton-row")).toHaveLength(5);
    expect(document.querySelector(".skeleton")).toHaveAttribute("aria-hidden", "true");

    expect(await screen.findByText("Loading tables…", {}, { timeout: 2000 })).toBeInTheDocument();
    expect(screen.getByText("Loading tables…").closest("[role=status]")).not.toBeNull();
    slow.release({ status: 200, body: PAGE });
    expect(await screen.findByRole("link", { name: "fact_sales" })).toBeInTheDocument();
    expect(document.querySelector(".skeleton")).toBeNull();
  });

  it("names the cause and offers Try again when the list fails", async () => {
    const { user, server } = renderShell("/catalog", {
      routes: {
        "GET /api/v1/tables": {
          status: 500,
          body: { detail: "The catalog database is not reachable. Check the server's log." },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The catalog database is not reachable. Check the server's log.",
    );
    server.route({ "GET /api/v1/tables": { status: 200, body: PAGE } });
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("link", { name: "fact_sales" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("invites to deploy when there are no tables yet", async () => {
    renderShell("/catalog", { routes: { "GET /api/v1/tables": { status: 200, body: EMPTY } } });

    expect(
      await screen.findByText(
        "No tables in all environments yet. Tables appear once a project is deployed.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("lakana deploy --env dev")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /tutorial/ })).toHaveAttribute(
      "href",
      expect.stringContaining("tutorials/getting-started"),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("names the place and its environment in the invitation", async () => {
    renderShell("/catalog?env=prod&ns=gold", {
      routes: { "GET /api/v1/tables": { status: 200, body: EMPTY } },
    });

    expect(
      await screen.findByText("No tables in gold yet. Tables appear once a project is deployed."),
    ).toBeInTheDocument();
    expect(screen.getByText("lakana deploy --env prod")).toBeInTheDocument();
  });

  it("moves focus to the first new row after Show more", async () => {
    const first = {
      items: [item({ name: "a" }), item({ name: "b" })],
      next_cursor: "c2",
      capped: false,
    };
    const second = { items: [item({ name: "c" })], next_cursor: null, capped: false };
    const { user } = renderShell("/catalog", {
      routes: {
        "GET /api/v1/tables": (request) => ({
          status: 200,
          body: request.path.includes("cursor=c2") ? second : first,
        }),
      },
    });
    await screen.findByRole("link", { name: "b" });
    expect(screen.getByRole("status")).toHaveTextContent("The first 2 tables");

    await user.click(screen.getByRole("button", { name: "Show more" }));

    await waitFor(() => {
      expect(screen.getByRole("link", { name: "c" })).toHaveFocus();
    });
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("3 tables");
  });

  it("names the cause beside Show more when a later page fails", async () => {
    const first = { items: [item({ name: "a" })], next_cursor: "c2", capped: false };
    const { user } = renderShell("/catalog", {
      routes: {
        "GET /api/v1/tables": (request) =>
          request.path.includes("cursor=c2")
            ? { status: 500, body: { detail: "The next page could not be read. Try again." } }
            : { status: 200, body: first },
      },
    });
    await screen.findByRole("link", { name: "a" });

    await user.click(screen.getByRole("button", { name: "Show more" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The next page could not be read.");
    expect(screen.getByRole("link", { name: "a" })).toBeInTheDocument();
    expect(within(screen.getByRole("alert")).queryByRole("button")).toBeNull();
    expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
  });

  it("says when the server stopped looking", async () => {
    renderShell("/catalog", {
      routes: { "GET /api/v1/tables": { status: 200, body: { ...PAGE, capped: true } } },
    });

    expect(
      await screen.findByText(
        "The search stopped after 5,000 tables. Use a longer text or choose a namespace.",
      ),
    ).toBeInTheDocument();
  });

  it("answers an unknown place with the not-found state", async () => {
    renderShell("/catalog/prod/gold/x/y");

    expect(await screen.findByRole("heading", { name: "This page does not exist" })).toBeVisible();
  });

  it("keeps focus in the page when a sort fails on its first page", async () => {
    const { user, server } = renderShell("/catalog");
    await screen.findByRole("link", { name: "fact_sales" });
    server.route({
      "GET /api/v1/tables": {
        status: 500,
        body: { detail: "The catalog database is not reachable. Check the server's log." },
      },
    });

    await user.click(screen.getByRole("button", { name: "Rows" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Try again" })).toHaveFocus();
    });
  });

  describe("search", () => {
    /**
     * The list requests sent so far.
     *
     * @param server - The fake server.
     * @returns Their paths.
     */
    function listed(server: ReturnType<typeof renderShell>["server"]): string[] {
      return server.sent.filter((r) => r.path.startsWith("/api/v1/tables?")).map((r) => r.path);
    }

    /**
     * Types into the search field.
     *
     * @param value - The text.
     */
    function type(value: string): void {
      fireEvent.change(screen.getByRole("searchbox"), { target: { value } });
    }

    it("searches within the chosen namespace after typing pauses", async () => {
      const { server, router } = renderShell("/catalog?env=prod&ns=gold");
      await screen.findByRole("link", { name: "fact_sales" });
      const before = listed(server).length;

      type("s");
      type("store");
      type("store_id");
      expect(listed(server)).toHaveLength(before);

      await waitFor(() => {
        expect(listed(server)).toHaveLength(before + 1);
      });
      expect(listed(server).at(-1)).toContain("environment=prod&namespace=gold&q=store_id");
      expect(router.state.location.search).toBe("?env=prod&ns=gold&q=store_id");
    });

    it("keeps typing in the search field while the address follows", async () => {
      const { user, router } = renderShell("/catalog");
      await screen.findByRole("link", { name: "fact_sales" });

      await user.type(screen.getByRole("searchbox"), "sales");

      await waitFor(() => {
        expect(router.state.location.search).toBe("?q=sales");
      });
      expect(router.state.historyAction).toBe("REPLACE");
      expect(screen.getByRole("searchbox")).toHaveFocus();
      expect(screen.getByRole("searchbox")).toHaveValue("sales");
    });

    it("shows a column hit on the second line with the match marked", async () => {
      const hit: TableMatchOut = {
        kind: "column",
        matched: "description",
        column: "store_id",
        column_type: "int",
        snippet: "joins dim_store",
        language: null,
        translated: false,
      };
      renderShell("/catalog?q=store_id", {
        routes: {
          "GET /api/v1/tables": {
            status: 200,
            body: {
              items: [item({ name: "visits", match: hit })],
              next_cursor: null,
              capped: false,
            },
          },
        },
      });

      const row = (await screen.findByRole("link", { name: "visits" })).closest("tr");
      expect(row).toHaveTextContent("Column store_id · int · joins dim_store");
      expect(row?.querySelector("mark")).toHaveTextContent("store_id");
      expect(row).not.toHaveTextContent("Table · gold");
    });

    it("keeps the normal second line for a hit on the name", async () => {
      const hit: TableMatchOut = {
        kind: "table",
        matched: "name",
        column: null,
        column_type: null,
        snippet: null,
        language: null,
        translated: false,
      };
      renderShell("/catalog?q=visits", {
        routes: {
          "GET /api/v1/tables": {
            status: 200,
            body: {
              items: [item({ name: "visits", match: hit })],
              next_cursor: null,
              capped: false,
            },
          },
        },
      });

      const row = (await screen.findByRole("link", { name: "visits" })).closest("tr");
      expect(row).toHaveTextContent("Table · gold");
    });

    it("says how many tables match in the place and offers Search all namespaces", async () => {
      renderShell("/catalog?env=prod&ns=gold&q=store");

      await screen.findByRole("link", { name: "fact_sales" });

      expect(screen.getByRole("status")).toHaveTextContent("3 tables in gold match");
      expect(screen.getByRole("button", { name: "Search all namespaces" })).toBeInTheDocument();
    });

    it("says it for all namespaces without offering to widen", async () => {
      renderShell("/catalog?q=store");

      await screen.findByRole("link", { name: "fact_sales" });

      expect(screen.getByRole("status")).toHaveTextContent("3 tables in all namespaces match");
      expect(
        screen.queryByRole("button", { name: "Search all namespaces" }),
      ).not.toBeInTheDocument();
    });

    it("widens to all namespaces and keeps the text", async () => {
      const { user, router, server } = renderShell("/catalog?env=prod&ns=gold&q=store_id");
      await screen.findByRole("link", { name: "fact_sales" });

      await user.click(screen.getByRole("button", { name: "Search all namespaces" }));

      expect(router.state.location.search).toBe("?q=store_id");
      expect(router.state.historyAction).toBe("PUSH");
      await waitFor(() => {
        expect(listed(server).at(-1)).toContain("q=store_id");
      });
      expect(listed(server).at(-1)).not.toContain("environment");
      expect(listed(server).at(-1)).not.toContain("namespace");
      expect(screen.getByRole("searchbox")).toHaveValue("store_id");
    });

    it("says when nothing matches and how to widen it", async () => {
      const { user, router } = renderShell("/catalog?env=prod&ns=gold&q=zzz", {
        routes: { "GET /api/v1/tables": { status: 200, body: EMPTY } },
      });

      expect(await screen.findByText("No table in gold matches “zzz”.")).toBeInTheDocument();
      expect(screen.queryByText(/Tables appear once/)).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Search all namespaces" }));

      expect(router.state.location.search).toBe("?q=zzz");
    });

    it("says when nothing matches anywhere and how to search better", async () => {
      renderShell("/catalog?q=zzz", {
        routes: { "GET /api/v1/tables": { status: 200, body: EMPTY } },
      });

      expect(
        await screen.findByText("No table in all namespaces matches “zzz”."),
      ).toBeInTheDocument();
      expect(
        screen.getByText(
          "Search looks at table and column names, labels and descriptions. Try a shorter text.",
        ),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Search all namespaces" }),
      ).not.toBeInTheDocument();
    });

    it("counts the first matches while more pages wait", async () => {
      renderShell("/catalog?q=store", {
        routes: {
          "GET /api/v1/tables": {
            status: 200,
            body: { items: [item({ name: "a" })], next_cursor: "c2", capped: false },
          },
        },
      });

      await screen.findByRole("link", { name: "a" });

      expect(screen.getByRole("status")).toHaveTextContent("The first 1 match in all namespaces");
    });
  });

  describe("places", () => {
    it("names an environment the person cannot read and links to all tables", async () => {
      renderShell("/catalog?env=staging");

      expect(
        await screen.findByText("There is no environment named staging that you may read."),
      ).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Show all tables" })).toHaveAttribute(
        "href",
        "/catalog",
      );
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
      expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    });

    it("names a namespace that is not in the environment", async () => {
      renderShell("/catalog?env=prod&ns=platinum");

      expect(
        await screen.findByText("There is no namespace platinum in prod that you may read."),
      ).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Show all tables" })).toBeInTheDocument();
    });

    it("shows no unknown-place sentence while the tree is on its way", async () => {
      const slow = held();
      renderShell("/catalog?env=staging", { routes: { "GET /api/v1/tables/tree": slow.route } });

      await screen.findByRole("heading", { level: 1, name: "Catalog" });

      expect(screen.queryByText(/There is no environment/)).not.toBeInTheDocument();
      slow.release({ status: 200, body: TREE });
      expect(await screen.findByText(/There is no environment/)).toBeInTheDocument();
    });

    it("says the person may read no environment yet", async () => {
      renderShell("/catalog", {
        routes: {
          "GET /api/v1/tables/tree": { status: 200, body: { count: 0, environments: [] } },
          "GET /api/v1/tables": { status: 200, body: EMPTY },
        },
      });

      expect(
        await screen.findByText(
          "You may read no environment yet. Ask an administrator for access.",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByRole("treegrid")).not.toBeInTheDocument();
      expect(screen.queryByText(/Tables appear once/)).not.toBeInTheDocument();
    });

    it("names the cause and offers Try again when the tree fails", async () => {
      const { user, server } = renderShell("/catalog", {
        routes: {
          "GET /api/v1/tables/tree": {
            status: 500,
            body: { detail: "The catalog database is not reachable. Check the server's log." },
          },
        },
      });
      await screen.findByRole("link", { name: "fact_sales" });

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("The catalog database is not reachable.");
      server.route({ "GET /api/v1/tables/tree": { status: 200, body: TREE } });
      await user.click(within(alert.closest("aside") as HTMLElement).getByRole("button"));

      expect(await screen.findByRole("treegrid")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("shows the skeleton, not rows, until the tree has arrived", async () => {
      const slow = held();
      const { server } = renderShell("/catalog", {
        routes: { "GET /api/v1/tables/tree": slow.route },
      });
      await screen.findByRole("heading", { level: 1, name: "Catalog" });
      await waitFor(() => {
        expect(server.sent.some((r) => r.path.startsWith("/api/v1/tables?"))).toBe(true);
      });

      expect(document.querySelectorAll(".skeleton-row")).toHaveLength(5);
      expect(screen.queryByRole("link", { name: "fact_sales" })).not.toBeInTheDocument();
      slow.release({ status: 200, body: TREE });
      expect(await screen.findByRole("link", { name: "fact_sales" })).toBeInTheDocument();
    });

    it("does not say there are no tables in an environment it is about to call unknown", async () => {
      const slow = held();
      renderShell("/catalog?env=staging", {
        routes: {
          "GET /api/v1/tables/tree": slow.route,
          "GET /api/v1/tables": { status: 200, body: EMPTY },
        },
      });
      await screen.findByRole("heading", { level: 1, name: "Catalog" });
      await waitFor(() => {
        expect(document.querySelectorAll(".skeleton-row")).toHaveLength(5);
      });

      expect(screen.queryByText(/No tables in/)).not.toBeInTheDocument();
      slow.release({ status: 200, body: TREE });
      expect(await screen.findByText(/There is no environment named staging/)).toBeInTheDocument();
      expect(screen.queryByText(/No tables in/)).not.toBeInTheDocument();
    });

    it("does not say there are no tables to a person who may read no environment", async () => {
      const slow = held();
      renderShell("/catalog", {
        routes: {
          "GET /api/v1/tables/tree": slow.route,
          "GET /api/v1/tables": { status: 200, body: EMPTY },
        },
      });
      await screen.findByRole("heading", { level: 1, name: "Catalog" });
      await waitFor(() => {
        expect(document.querySelectorAll(".skeleton-row")).toHaveLength(5);
      });

      expect(screen.queryByText(/No tables in/)).not.toBeInTheDocument();
      slow.release({ status: 200, body: { count: 0, environments: [] } });
      expect(await screen.findByText(/You may read no environment yet/)).toBeInTheDocument();
      expect(screen.queryByText(/No tables in/)).not.toBeInTheDocument();
    });

    it("keeps no empty complementary landmark before the tree arrives", async () => {
      const slow = held();
      renderShell("/catalog", { routes: { "GET /api/v1/tables/tree": slow.route } });

      await screen.findByRole("heading", { level: 1, name: "Catalog" });

      expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
      slow.release({ status: 200, body: TREE });
      expect(await screen.findByRole("treegrid")).toBeInTheDocument();
    });
  });

  describe("last choice", () => {
    const KEY = "lakana.catalog.admin";

    it("restores the last choice when the address is empty", async () => {
      window.localStorage.setItem(KEY, JSON.stringify({ environment: "prod", namespace: "gold" }));
      const { router, server } = renderShell("/catalog");

      await waitFor(() => {
        expect(router.state.location.search).toBe("?env=prod&ns=gold");
      });
      expect(router.state.historyAction).toBe("REPLACE");
      await screen.findByRole("link", { name: "fact_sales" });
      const lists = server.sent.filter((r) => r.path.startsWith("/api/v1/tables?"));
      expect(lists.length).toBeGreaterThan(0);
      for (const list of lists) {
        expect(list.path).toContain("environment=prod&namespace=gold");
      }
    });

    it("lets a link win over the last choice", async () => {
      window.localStorage.setItem(KEY, JSON.stringify({ environment: "prod", namespace: "gold" }));
      const { router } = renderShell("/catalog?env=dev");
      await screen.findByRole("treegrid");

      expect(router.state.location.search).toBe("?env=dev");
      await waitFor(() => {
        expect(JSON.parse(window.localStorage.getItem(KEY) ?? "null")).toEqual({
          environment: "dev",
          namespace: null,
        });
      });
    });

    it("forgets a remembered place the tree no longer has", async () => {
      window.localStorage.setItem(KEY, JSON.stringify({ environment: "staging", namespace: null }));
      const { router } = renderShell("/catalog");
      await screen.findByRole("treegrid");

      await waitFor(() => {
        expect(router.state.location.search).toBe("");
      });
      await waitFor(() => {
        expect(JSON.parse(window.localStorage.getItem(KEY) ?? "null")).toEqual({
          environment: null,
          namespace: null,
        });
      });
      expect(screen.queryByText(/There is no environment/)).not.toBeInTheDocument();
      expect(router.state.historyAction).toBe("REPLACE");
      expect(await screen.findByRole("link", { name: "fact_sales" })).toBeInTheDocument();
    });

    it("remembers the place that was chosen and restores it once only", async () => {
      window.localStorage.setItem(KEY, JSON.stringify({ environment: "prod", namespace: "gold" }));
      const { user, router } = renderShell("/catalog");
      await waitFor(() => {
        expect(router.state.location.search).toBe("?env=prod&ns=gold");
      });
      await screen.findByRole("treegrid");

      await user.click(
        within(screen.getByRole("row", { name: "All tables, 9 tables" })).getByRole("link"),
      );

      expect(router.state.location.search).toBe("");
      await waitFor(() => {
        expect(JSON.parse(window.localStorage.getItem(KEY) ?? "null")).toEqual({
          environment: null,
          namespace: null,
        });
      });
      expect(router.state.location.search).toBe("");
    });

    it("restores the last choice when the sidebar's Catalog is pressed on the Catalog", async () => {
      window.localStorage.setItem(KEY, JSON.stringify({ environment: "prod", namespace: "gold" }));
      const { user, router } = renderShell("/catalog?env=prod&ns=gold");
      await screen.findByRole("treegrid");

      await user.click(
        within(screen.getByRole("navigation")).getByRole("link", { name: "Catalog" }),
      );

      await waitFor(() => {
        expect(router.state.location.search).toBe("?env=prod&ns=gold");
      });
      expect(JSON.parse(window.localStorage.getItem(KEY) ?? "null")).toEqual({
        environment: "prod",
        namespace: "gold",
      });
      expect(
        within(screen.getByRole("row", { name: "gold, 4 tables" })).getByRole("link"),
      ).toHaveAttribute("aria-current", "page");
    });

    it("does not restore the last choice on Back to the empty address", async () => {
      const { user, router } = renderShell("/catalog");
      await screen.findByRole("treegrid");
      await user.click(
        within(screen.getByRole("row", { name: "prod, 6 tables" })).getByRole("link"),
      );
      expect(router.state.location.search).toBe("?env=prod");

      await router.navigate(-1);

      await waitFor(() => {
        expect(
          within(screen.getByRole("row", { name: "All tables, 9 tables" })).getByRole("link"),
        ).toHaveAttribute("aria-current", "page");
      });
      expect(router.state.location.search).toBe("");
    });

    it("keeps nothing for an unknown place in the address", async () => {
      renderShell("/catalog?env=staging");
      await screen.findByText(/There is no environment/);

      expect(window.localStorage.getItem(KEY)).toBeNull();
    });

    it("restores the view on Back and Forward", async () => {
      const { user, router } = renderShell("/catalog?q=store");
      await screen.findByRole("treegrid");

      await user.click(
        within(screen.getByRole("row", { name: "prod, 6 tables" })).getByRole("link"),
      );
      await user.click(screen.getByRole("button", { name: "Rows" }));
      expect(router.state.location.search).toBe("?env=prod&q=store&sort=rows");

      await router.navigate(-1);
      await waitFor(() => {
        expect(router.state.location.search).toBe("?env=prod&q=store");
        expect(
          within(screen.getByRole("row", { name: "prod, 6 tables" })).getByRole("link"),
        ).toHaveAttribute("aria-current", "page");
      });
      expect(screen.getByRole("searchbox")).toHaveValue("store");
      await router.navigate(-1);
      await waitFor(() => {
        expect(router.state.location.search).toBe("?q=store");
        expect(
          within(screen.getByRole("row", { name: "All tables, 9 tables" })).getByRole("link"),
        ).toHaveAttribute("aria-current", "page");
      });
      await router.navigate(1);
      await waitFor(() => {
        expect(router.state.location.search).toBe("?env=prod&q=store");
      });
    });
  });
});
