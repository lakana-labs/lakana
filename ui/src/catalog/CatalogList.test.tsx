import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { item, PAGE } from "../test/catalog";
import { held } from "../test/pending";
import { renderShell } from "../test/render";

const EMPTY = { items: [], next_cursor: null, capped: false };

/**
 * The list's rows below the head.
 *
 * @returns The rows.
 */
function rows(): HTMLElement[] {
  return screen.getAllByRole("row").slice(1);
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
});
