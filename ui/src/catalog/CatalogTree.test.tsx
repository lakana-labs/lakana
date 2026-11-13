import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../test/render";

/**
 * The tree's node of a name. React Aria draws a navigation tree as a `treegrid` of `row`s.
 *
 * @param name - The node's accessible name, such as "gold, 4 tables".
 * @returns The node.
 */
function node(name: string): HTMLElement {
  return screen.getByRole("row", { name });
}

/**
 * The request the list sent last.
 *
 * @param server - The fake server.
 * @returns Its path.
 */
function lastList(server: ReturnType<typeof renderShell>["server"]): string {
  return server.sent.filter((r) => r.path.startsWith("/api/v1/tables?")).at(-1)?.path ?? "";
}

describe("CatalogTree", () => {
  it("is a tree named Environments and namespaces with All tables first", async () => {
    renderShell("/catalog");

    const tree = await screen.findByRole("treegrid", { name: "Environments and namespaces" });
    expect(within(tree).getAllByRole("row")[0]).toHaveAccessibleName("All tables, 9 tables");
  });

  it("opens environments and keeps namespaces closed at first", async () => {
    const { user } = renderShell("/catalog");
    await screen.findByRole("treegrid");

    expect(node("dev, 3 tables")).toHaveAttribute("aria-expanded", "true");
    expect(node("prod, 6 tables")).toHaveAttribute("aria-expanded", "true");
    expect(node("gold, 4 tables")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("row", { name: "sales, 1 table" })).not.toBeInTheDocument();

    await user.click(within(node("gold, 4 tables")).getByRole("button"));

    expect(node("gold, 4 tables")).toHaveAttribute("aria-expanded", "true");
    expect(node("sales, 1 table")).toBeInTheDocument();
  });

  it("filters the list to a namespace and those beneath it", async () => {
    const { user, router, server } = renderShell("/catalog");
    await screen.findByRole("treegrid");

    await user.click(within(node("gold, 4 tables")).getByRole("link"));

    expect(router.state.location.search).toBe("?env=prod&ns=gold");
    await waitFor(() => {
      expect(lastList(server)).toContain("environment=prod&namespace=gold");
    });
    expect(within(node("gold, 4 tables")).getByRole("link")).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("chooses an environment and All tables", async () => {
    const { user, router } = renderShell("/catalog?env=dev&ns=gold");
    await screen.findByRole("treegrid");

    await user.click(within(node("prod, 6 tables")).getByRole("link"));
    expect(router.state.location.search).toBe("?env=prod");
    await user.click(within(node("All tables, 9 tables")).getByRole("link"));

    expect(router.state.location.search).toBe("");
    expect(within(node("All tables, 9 tables")).getByRole("link")).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("marks the chosen node and opens its ancestors on a deep link", async () => {
    renderShell("/catalog?env=prod&ns=gold.sales");
    await screen.findByRole("treegrid");

    expect(node("gold, 4 tables")).toHaveAttribute("aria-expanded", "true");
    expect(within(node("sales, 1 table")).getByRole("link")).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(node("gold, 4 tables")).getByRole("link")).not.toHaveAttribute("aria-current");
  });

  it("opens the ancestors when the view moves deeper", async () => {
    const { router } = renderShell("/catalog");
    await screen.findByRole("treegrid");
    expect(screen.queryByRole("row", { name: "sales, 1 table" })).not.toBeInTheDocument();

    await router.navigate("/catalog?env=prod&ns=gold.sales");

    expect(await screen.findByRole("row", { name: "sales, 1 table" })).toBeInTheDocument();
  });

  it("marks the node for an address written another way", async () => {
    renderShell("/catalog?ns=gold&sort=name&env=prod&order=up&junk=1");
    await screen.findByRole("treegrid");

    expect(within(node("gold, 4 tables")).getByRole("link")).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("moves through the tree with the arrow keys and chooses with Enter", async () => {
    const { user, router } = renderShell("/catalog");
    await screen.findByRole("treegrid");

    act(() => {
      within(node("All tables, 9 tables")).getByRole("link").focus();
    });
    await user.keyboard("{ArrowDown}");
    expect(within(node("dev, 3 tables")).getByRole("link")).toHaveFocus();
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(within(node("gold, 2 tables")).getByRole("link")).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(router.state.location.search).toBe("?env=dev&ns=gold");
  });

  it("keeps the search and the sort when another node is chosen", async () => {
    const { user, router } = renderShell("/catalog?q=store&sort=rows");
    await screen.findByRole("treegrid");

    await user.click(within(node("prod, 6 tables")).getByRole("link"));

    expect(router.state.location.search).toBe("?env=prod&q=store&sort=rows");
  });

  it("names a node's link with its count, as its row", async () => {
    renderShell("/catalog");
    await screen.findByRole("treegrid");

    expect(screen.getByRole("link", { name: "gold, 4 tables" })).toBeInTheDocument();
  });

  it("opens a node in a new tab from its link", async () => {
    renderShell("/catalog?q=store");
    await screen.findByRole("treegrid");

    expect(within(node("prod, 6 tables")).getByRole("link")).toHaveAttribute(
      "href",
      "/catalog?env=prod&q=store",
    );
  });

  it("writes the count quietly beside the label", async () => {
    renderShell("/catalog");
    await screen.findByRole("treegrid");

    expect(node("prod, 6 tables")).toHaveTextContent("prod· 6");
  });
});
