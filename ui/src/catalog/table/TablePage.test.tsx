import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { held } from "../../test/pending";
import { renderShell } from "../../test/render";
import { permissions, table, TABLE_PATH, tableRoutes } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const RECENT = "POST /api/v1/principals/admin/recent";

/**
 * The page's heading, once the page has arrived.
 *
 * @param name - The heading's text.
 * @returns The heading.
 */
async function heading(name = "fact_sales"): Promise<HTMLElement> {
  return screen.findByRole("heading", { level: 1, name });
}

/**
 * The tab links, once the table has arrived.
 *
 * @returns The links, in order.
 */
async function tabs(): Promise<HTMLElement[]> {
  const row = await screen.findByRole("navigation", { name: "Sections of the table" });
  return within(row).getAllByRole("link");
}

describe("TablePage", () => {
  describe("the frame", () => {
    it("shows the breadcrumb Catalog, environment, namespace and table", async () => {
      renderShell(PAGE);

      const trail = await screen.findByRole("navigation", { name: "Breadcrumb" });
      expect(
        within(trail)
          .getAllByRole("listitem")
          .map((item) => item.textContent),
      ).toEqual(["Catalog›", "prod›", "gold.sales›", "fact_sales"]);
      expect(within(trail).getByText("fact_sales")).toHaveAttribute("aria-current", "page");
    });

    it("links every breadcrumb part to its place", async () => {
      renderShell(PAGE);

      const trail = await screen.findByRole("navigation", { name: "Breadcrumb" });
      expect(within(trail).getByRole("link", { name: "Catalog" })).toHaveAttribute(
        "href",
        "/catalog",
      );
      expect(within(trail).getByRole("link", { name: "prod" })).toHaveAttribute(
        "href",
        "/catalog?env=prod",
      );
      expect(within(trail).getByRole("link", { name: "gold.sales" })).toHaveAttribute(
        "href",
        "/catalog?env=prod&ns=gold.sales",
      );
    });

    it("decodes a name and a namespace the address encodes", async () => {
      renderShell("/catalog/prod/gold.sales/50%25");

      expect(await heading("50%")).toBeVisible();
    });

    it("names kind, namespace and environment under the heading", async () => {
      renderShell(PAGE);

      await heading();
      const line = await screen.findByText("Fact · gold.sales");
      expect(within(line).getByText("prod")).toHaveClass("env-label");
    });

    it("shows the label when the table has one", async () => {
      renderShell(PAGE, {
        routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ label: "Sales facts" }) } },
      });

      expect(await screen.findByText("Sales facts")).toBeVisible();
    });

    it("shows Overview, Columns, Preview and Lineage in order, each with its own address", async () => {
      renderShell(PAGE);

      const links = await tabs();
      expect(links.map((link) => link.textContent)).toEqual([
        "Overview",
        "Columns",
        "Preview",
        "Lineage",
      ]);
      expect(links.map((link) => link.getAttribute("href"))).toEqual([
        PAGE,
        `${PAGE}/columns`,
        `${PAGE}/preview`,
        `${PAGE}/lineage`,
      ]);
      expect(links[0]).toHaveAttribute("aria-current", "page");
    });

    it.each([
      ["columns", "Columns"],
      ["preview", "Preview"],
      ["lineage", "Lineage"],
    ])("opens the %s tab at its address", async (segment, name) => {
      renderShell(`${PAGE}/${segment}`);

      const links = await tabs();
      expect(links.find((link) => link.textContent === name)).toHaveAttribute(
        "aria-current",
        "page",
      );
      expect(screen.getByRole("region", { name })).toBeInTheDocument();
    });

    it("moves between tabs and keeps the table", async () => {
      const { user, router } = renderShell(PAGE);

      await user.click((await tabs())[1] as HTMLElement);

      expect(router.state.location.pathname).toBe(`${PAGE}/columns`);
      expect(await heading()).toHaveFocus();
    });

    it("reads the definition once, however often the current tab is pressed", async () => {
      const { user, server } = renderShell(PAGE);
      const reads = (): number =>
        server.sent.filter((request) => request.path === `${TABLE_PATH}/definition`).length;
      await screen.findByText(/line 1/);
      await waitFor(() => {
        expect(reads()).toBe(1);
      });

      await user.click((await tabs())[0] as HTMLElement);
      await user.click((await tabs())[0] as HTMLElement);

      expect(reads()).toBe(1);
    });

    it("shows the same tabs to a reader who is not an administrator", async () => {
      renderShell(PAGE, {
        me: { name: "kim", admin: false, mayDeploy: false },
        routes: {
          ...tableRoutes("kim"),
          "GET /api/v1/principals/kim/permissions": {
            status: 200,
            body: permissions("kim", ["READ_TABLE"]),
          },
        },
      });

      expect((await tabs()).map((link) => link.textContent)).toEqual([
        "Overview",
        "Columns",
        "Preview",
        "Lineage",
      ]);
    });

    it("offers no primary action", async () => {
      renderShell(PAGE);

      await tabs();
      const head = (await heading()).parentElement as HTMLElement;
      expect(head.children).toHaveLength(1);
    });

    it("answers an unknown tab with the not-found state", async () => {
      renderShell(`${PAGE}/lineage-or-so`);

      expect(await heading("This page does not exist")).toBeVisible();
    });

    it("answers the address of Overview with a suffix with the not-found state", async () => {
      renderShell(`${PAGE}/overview`);

      expect(await heading("This page does not exist")).toBeVisible();
    });
  });

  describe("the states", () => {
    it("says the table is gone and links back to the Catalog, and records nothing", async () => {
      const { server } = renderShell(PAGE, {
        routes: { [`GET ${TABLE_PATH}`]: { status: 404, body: { detail: "No such table." } } },
      });

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "gold.sales.fact_sales is no longer in prod; it may have been dropped or renamed.",
      );
      expect(screen.getByRole("link", { name: "Back to the Catalog" })).toHaveAttribute(
        "href",
        "/catalog",
      );
      expect(screen.queryByRole("link", { name: "Columns" })).not.toBeInTheDocument();
      expect(posted(server)).toEqual([]);
    });

    it("says the person may not read the table or it does not exist, and links back, recording nothing", async () => {
      const { server } = renderShell(PAGE, {
        routes: { [`GET ${TABLE_PATH}`]: { status: 403, body: { detail: "Not allowed." } } },
      });

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "You may not read gold.sales.fact_sales, or there is no table by that name. Check the name, or ask an administrator for access.",
      );
      expect(screen.getByRole("link", { name: "Back to the Catalog" })).toHaveAttribute(
        "href",
        "/catalog",
      );
      expect(posted(server)).toEqual([]);
    });

    it("says the storage cannot be reached and offers Try again, then moves focus to the heading", async () => {
      let calls = 0;
      const { user } = renderShell(PAGE, {
        routes: {
          [`GET ${TABLE_PATH}`]: () => {
            calls += 1;
            return calls === 1
              ? { status: 503, body: { detail: "Storage is down." } }
              : { status: 200, body: table() };
          },
        },
      });

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "The table's storage cannot be reached right now. Try again in a moment.",
      );
      await user.click(screen.getByRole("button", { name: "Try again" }));

      expect(await heading()).toHaveFocus();
      await tabs();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(await heading()).toHaveFocus();
    });

    it("names the cause of any other failure", async () => {
      renderShell(PAGE, {
        routes: {
          [`GET ${TABLE_PATH}`]: { status: 500, body: { detail: "The catalog database broke." } },
        },
      });

      expect(await screen.findByRole("alert")).toHaveTextContent("The catalog database broke.");
      expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    });

    it("shows the heading from the address while the table is read, then says it is loading", async () => {
      const slow = held();
      renderShell(PAGE, { routes: { [`GET ${TABLE_PATH}`]: slow.route } });

      expect(await heading()).toBeVisible();
      expect(screen.queryByText("Loading the table…")).not.toBeInTheDocument();
      expect(await screen.findByText("Loading the table…", {}, { timeout: 2500 })).toHaveAttribute(
        "role",
        "status",
      );
      expect(screen.queryByRole("link", { name: "Columns" })).not.toBeInTheDocument();
    });
  });

  describe("focus", () => {
    it("focuses the table's heading after opening it from the list, never the body", async () => {
      const { user } = renderShell("/catalog");

      await user.click(await screen.findByRole("link", { name: "fact_sales" }));

      expect(await heading()).toHaveFocus();
      expect(document.body).not.toHaveFocus();
    });

    it("keeps the focus on the heading while the table is still being read", async () => {
      const slow = held();
      const { user } = renderShell("/catalog", {
        routes: { [`GET ${TABLE_PATH}`]: slow.route },
      });

      await user.click(await screen.findByRole("link", { name: "fact_sales" }));
      expect(await heading()).toHaveFocus();

      await act(async () => {
        slow.release({ status: 200, body: table() });
        await Promise.resolve();
      });
      await tabs();
      expect(await heading()).toHaveFocus();
    });
  });

  describe("recently opened", () => {
    it("records the table as opened once", async () => {
      const { server } = renderShell(PAGE);

      await tabs();

      await waitFor(() => {
        expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(1);
      });
      expect(server.sent.find((request) => request.method === "POST")).toMatchObject({
        path: "/api/v1/principals/admin/recent",
        body: { kind: "table", environment: "prod", ref: "gold.sales.fact_sales" },
      });
    });

    it("records each table opened in turn and shows the new one", async () => {
      const { router, server } = renderShell(PAGE, {
        routes: {
          "GET /api/v1/environments/prod/namespaces/gold/tables/dim_store": {
            status: 200,
            body: table({ namespace: "gold", name: "dim_store", model: null }),
          },
        },
      });
      await tabs();

      await act(async () => {
        await router.navigate("/catalog/prod/gold/dim_store");
      });

      expect(await heading("dim_store")).toBeVisible();
      await waitFor(() => {
        expect(posted(server)).toEqual(["gold.sales.fact_sales", "gold.dim_store"]);
      });

      await act(async () => {
        await router.navigate(-1);
      });

      expect(await heading()).toBeVisible();
      await waitFor(() => {
        expect(posted(server)).toEqual([
          "gold.sales.fact_sales",
          "gold.dim_store",
          "gold.sales.fact_sales",
        ]);
      });
    });

    it("still shows the page when recording fails", async () => {
      renderShell(PAGE, {
        routes: { [RECENT]: { status: 500, body: { detail: "No record." } } },
      });

      expect((await tabs()).map((link) => link.textContent)).toEqual([
        "Overview",
        "Columns",
        "Preview",
        "Lineage",
      ]);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  });
});

/**
 * The tables recorded as opened, in order.
 *
 * @param server - The fake server.
 * @param server.sent - Every request.
 * @returns The `ref` of each record.
 */
function posted(server: { sent: { method: string; body?: unknown }[] }): string[] {
  return server.sent
    .filter((request) => request.method === "POST")
    .map((request) => (request.body as { ref: string }).ref);
}
