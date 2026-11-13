import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../../test/render";
import { table, TABLE_PATH } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";

describe("Overview", () => {
  it("shows the description and where it comes from", async () => {
    renderShell(PAGE);

    expect(await screen.findByText("Every sale in every store, one row per sale.")).toBeVisible();
    expect(screen.getByText("From the project in Git")).toBeVisible();
  });

  it("says a description was edited here", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ description_source: "edited" }),
        },
      },
    });

    expect(await screen.findByText("Edited here")).toBeVisible();
    expect(screen.queryByText("From the project in Git")).not.toBeInTheDocument();
  });

  it("says there is no description yet, without a source", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ description: null, description_source: null }),
        },
      },
    });

    expect(await screen.findByText("No description yet.")).toBeVisible();
    expect(screen.queryByText("From the project in Git")).not.toBeInTheDocument();
    expect(screen.queryByText("Edited here")).not.toBeInTheDocument();
  });

  it("shows the first five columns and links to all", async () => {
    renderShell(PAGE);

    const heading = await screen.findByRole("heading", { level: 2, name: "Columns" });
    const block = heading.parentElement as HTMLElement;
    const names = within(block)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[0]?.textContent);
    expect(names).toEqual(["sale_id", "store_id", "sold_at", "amount", "customer_email"]);
    expect(within(block).getByRole("link", { name: "All columns ›" })).toHaveAttribute(
      "href",
      `${PAGE}/columns`,
    );
  });

  it("shows each column's type with its role", async () => {
    renderShell(PAGE);

    const heading = await screen.findByRole("heading", { level: 2, name: "Columns" });
    const rows = within(heading.parentElement as HTMLElement).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("sale_id");
    expect(rows[1]).toHaveTextContent("long");
    expect(rows[1]).toHaveTextContent("Key");
    expect(rows[4]).toHaveTextContent("decimal(10,2)");
    expect(rows[4]).toHaveTextContent("Measure");
  });

  it("does not run a preview", async () => {
    const { server } = renderShell(PAGE);
    await screen.findByText("Every sale in every store, one row per sale.");

    await waitFor(() => {
      expect(server.sent.some((request) => request.path.includes("/health"))).toBe(true);
    });
    expect(server.sent.some((request) => request.path.split("?")[0]?.endsWith("/preview"))).toBe(
      false,
    );
  });

  it("reads health without readers", async () => {
    const { server } = renderShell(PAGE);

    await screen.findByRole("heading", { level: 2, name: "Health" });

    await waitFor(() => {
      expect(
        server.sent.some((request) => request.path.endsWith("/health?days=7&readers=false")),
      ).toBe(true);
    });
    expect(server.sent.some((request) => request.path.includes("readers=true"))).toBe(false);
  });

  it("shows the sections in the order of the page", async () => {
    renderShell(PAGE);

    await screen.findByRole("heading", { level: 2, name: "Health" });
    const names = screen
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);
    expect(names).toEqual(["Description", "Columns", "Health", "Facts", "Comes from · goes to"]);
  });
});
