import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { treeWith, TREE } from "../../test/catalog";
import { manageRoutes } from "../../test/manage";
import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { buildTree, type CatalogNode, findNode } from "../tree";
import PlaceActions from "./PlaceActions";

/**
 * A place of the administrator's tree.
 *
 * @param environment - The environment.
 * @param namespace - The namespace; null for the environment.
 * @returns The node.
 */
function place(environment: string, namespace: string | null): CatalogNode {
  const node = findNode(buildTree(TREE), environment, namespace);
  if (node === null) {
    throw new Error(`no place ${environment} ${String(namespace)}`);
  }
  return node;
}

/** An empty namespace with every action, which may be deleted. */
const EMPTY: CatalogNode = {
  id: "ns:prod:silver",
  label: "silver",
  environment: "prod",
  namespace: "silver",
  count: 0,
  actions: ["create_namespace", "create_external_table", "drop"],
  children: [],
};

/**
 * Renders the menu for a place.
 *
 * @param node - The place.
 * @returns The handlers, the rerender and a user-event session.
 */
function renderMenu(node: CatalogNode) {
  const onCreated = vi.fn();
  const onDeleted = vi.fn();
  const onChanged = vi.fn();
  const onFocusLost = vi.fn();
  const user = userEvent.setup();
  const rendered = render(
    <PlaceActions
      key={node.id}
      node={node}
      onCreated={onCreated}
      onDeleted={onDeleted}
      onChanged={onChanged}
      onFocusLost={onFocusLost}
    />,
  );
  return { onCreated, onDeleted, onChanged, onFocusLost, user, ...rendered };
}

/**
 * The items of the open menu, by their names.
 *
 * @returns The names, in order.
 */
function itemNames(): string[] {
  return within(screen.getByRole("menu"))
    .getAllByRole("menuitem")
    .map((item) => item.textContent);
}

describe("PlaceActions", () => {
  it("offers exactly the items actions lists, in the order New namespace, New external table, Delete namespace", async () => {
    const { user, unmount } = renderMenu({
      ...EMPTY,
      actions: ["drop", "create_external_table", "create_namespace"],
    });

    await user.click(screen.getByRole("button", { name: "Actions for silver" }));

    expect(itemNames()).toEqual(["New namespace…", "New external table…", "Delete namespace…"]);
    expect(within(screen.getByRole("menu")).getByRole("separator")).toBeInTheDocument();
    unmount();

    const second = renderMenu({ ...EMPTY, actions: ["drop"] });
    await second.user.click(screen.getByRole("button", { name: "Actions for silver" }));
    expect(itemNames()).toEqual(["Delete namespace…"]);
    expect(within(screen.getByRole("menu")).queryByRole("separator")).not.toBeInTheDocument();
  });

  it("offers only New namespace on an environment and renders nothing without actions", async () => {
    const environment = place("prod", null);
    const { user, unmount } = renderMenu({
      ...environment,
      actions: ["create_namespace", "create_external_table", "drop"],
    });

    await user.click(screen.getByRole("button", { name: "Actions for prod" }));

    expect(itemNames()).toEqual(["New namespace…"]);
    unmount();

    const reader = findNode(buildTree(treeWith([])), "prod", "gold");
    const none = renderMenu(reader ?? EMPTY);
    expect(none.container).toBeEmptyDOMElement();
    none.unmount();

    const all = renderMenu({ ...buildTree(TREE), actions: ["create_namespace"] });
    expect(all.container).toBeEmptyDOMElement();
  });

  it("disables Delete on a namespace with tables or children and the menu is described by Only an empty namespace can be deleted", async () => {
    for (const node of [place("prod", "gold.sales"), { ...place("prod", "gold"), count: 0 }]) {
      const { user, unmount } = renderMenu(node);
      await user.click(screen.getByRole("button", { name: /^Actions for/ }));

      const menu = screen.getByRole("menu");
      expect(within(menu).getByRole("menuitem", { name: "Delete namespace…" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
      expect(menu).toHaveAccessibleDescription("Only an empty namespace can be deleted.");
      unmount();
    }

    const { user } = renderMenu(EMPTY);
    await user.click(screen.getByRole("button", { name: "Actions for silver" }));
    expect(screen.getByRole("menu")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByRole("menuitem", { name: "Delete namespace…" })).not.toHaveAttribute(
      "aria-disabled",
    );
  });

  it("names the button Actions for {place}, with a bidi mark visible", () => {
    renderMenu({ ...EMPTY, id: "ns:prod:gold\u202Esales", namespace: "gold\u202Esales" });

    const button = screen.getByRole("button", { name: "Actions for goldU+202Esales" });
    expect(button).toHaveTextContent("Actions ▾");
    expect(within(button).getByText("▾")).toHaveAttribute("aria-hidden", "true");
  });

  it("Escape on a dialog returns the focus to Actions", async () => {
    fakeServer(manageRoutes());
    const { user } = renderMenu(EMPTY);
    const button = screen.getByRole("button", { name: "Actions for silver" });

    for (const item of ["New namespace…", "Delete namespace…"]) {
      await user.click(button);
      await user.click(screen.getByRole("menuitem", { name: item }));
      expect(await screen.findByRole("dialog")).toBeInTheDocument();
      await user.keyboard("{Escape}");
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
        expect(button).toHaveFocus();
      });
    }
  });

  it("opens New external table only on a namespace and returns the focus to Actions on Cancel", async () => {
    fakeServer(manageRoutes());
    const bronze: CatalogNode = {
      ...EMPTY,
      id: "ns:prod:bronze",
      label: "bronze",
      namespace: "bronze",
    };
    const user = userEvent.setup();
    const router = createMemoryRouter(
      [
        {
          path: "/catalog/prod/bronze",
          element: (
            <PlaceActions
              node={bronze}
              onCreated={vi.fn()}
              onDeleted={vi.fn()}
              onChanged={vi.fn()}
              onFocusLost={vi.fn()}
            />
          ),
        },
      ],
      { initialEntries: ["/catalog/prod/bronze"] },
    );
    render(<RouterProvider router={router} />);
    const button = screen.getByRole("button", { name: "Actions for bronze" });

    await user.click(button);
    await user.click(screen.getByRole("menuitem", { name: "New external table…" }));
    expect(await screen.findByRole("dialog")).toHaveAccessibleName("New external table in bronze");
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(button).toHaveFocus();
    });
    cleanup();

    const environment = renderMenu({ ...place("prod", null), actions: EMPTY.actions });
    await environment.user.click(screen.getByRole("button", { name: "Actions for prod" }));
    expect(
      within(screen.getByRole("menu")).queryByRole("menuitem", { name: "New external table…" }),
    ).not.toBeInTheDocument();
  });

  it("closes New external table when the place changes and does not go to the table on a late answer", async () => {
    const slow = held();
    fakeServer({
      ...manageRoutes(),
      "POST /api/v1/environments/prod/namespaces/silver/tables/external": slow.route,
    });
    const onChanged = vi.fn();
    const onFocusLost = vi.fn();
    const user = userEvent.setup();
    const menu = (node: CatalogNode) => (
      <PlaceActions
        key={node.id}
        node={node}
        onCreated={vi.fn()}
        onDeleted={vi.fn()}
        onChanged={onChanged}
        onFocusLost={onFocusLost}
      />
    );
    const router = createMemoryRouter(
      [
        { path: "/catalog/prod/silver", element: menu(EMPTY) },
        { path: "/catalog/prod/gold", element: menu(place("prod", "gold")) },
        { path: "/catalog/:environment/:namespace/:name", element: <p>The table</p> },
      ],
      { initialEntries: ["/catalog/prod/silver"] },
    );
    render(<RouterProvider router={router} />);

    await user.click(screen.getByRole("button", { name: "Actions for silver" }));
    await user.click(screen.getByRole("menuitem", { name: "New external table…" }));
    await user.type(await screen.findByRole("textbox", { name: "Name" }), "orders");
    await user.type(screen.getByRole("textbox", { name: "Location" }), "orders/*.csv");
    await user.click(await screen.findByRole("button", { name: "Register" }));
    await router.navigate("/catalog/prod/gold");

    // The router renders the new place in a transition.
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(onFocusLost).toHaveBeenCalledTimes(1);
    slow.release({
      status: 201,
      body: {
        name: "orders",
        namespace: "silver",
        location_pattern: "raw/orders/*.csv",
        format: "csv",
        schema: {},
      },
    });
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(router.state.location.pathname).toBe("/catalog/prod/gold");
  });

  it("calls onCreated with the new path and onDeleted with the deleted one", async () => {
    fakeServer(manageRoutes());
    const { onCreated, onDeleted, user } = renderMenu(EMPTY);
    const button = screen.getByRole("button", { name: "Actions for silver" });

    await user.click(button);
    await user.click(screen.getByRole("menuitem", { name: "New namespace…" }));
    await user.type(await screen.findByRole("textbox", { name: "Path" }), "raw{Enter}");
    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith("silver.raw");
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(button);
    await user.click(screen.getByRole("menuitem", { name: "Delete namespace…" }));
    await user.click(await screen.findByRole("button", { name: "Delete the namespace" }));
    await waitFor(() => {
      expect(onDeleted).toHaveBeenCalledWith("silver");
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes the dialog when the place changes and does not navigate on a late answer", async () => {
    const slow = held();
    fakeServer({ ...manageRoutes(), "POST /api/v1/environments/prod/namespaces": slow.route });
    const { onCreated, onChanged, onFocusLost, user, rerender } = renderMenu(EMPTY);

    await user.click(screen.getByRole("button", { name: "Actions for silver" }));
    await user.click(screen.getByRole("menuitem", { name: "New namespace…" }));
    await user.type(await screen.findByRole("textbox", { name: "Path" }), "raw");
    await user.click(await screen.findByRole("button", { name: "Create" }));

    const other = place("prod", "gold");
    rerender(
      <PlaceActions
        key={other.id}
        node={other}
        onCreated={onCreated}
        onDeleted={vi.fn()}
        onChanged={onChanged}
        onFocusLost={onFocusLost}
      />,
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // The page puts the focus somewhere that stays (CatalogList's tests follow it there).
    expect(onFocusLost).toHaveBeenCalledTimes(1);
    slow.release({ status: 201, body: { path: "silver.raw", properties: {} } });
    await waitFor(() => {
      expect(onChanged).toHaveBeenCalledTimes(1);
    });
    expect(onCreated).not.toHaveBeenCalled();
  });
  it("tells the page it lost the focus only when a change of place closed an open dialog", async () => {
    fakeServer(manageRoutes());
    const { onFocusLost, user, rerender } = renderMenu(EMPTY);
    const other = place("prod", "gold");
    const again = (node: CatalogNode) => {
      rerender(
        <PlaceActions
          key={node.id}
          node={node}
          onCreated={vi.fn()}
          onDeleted={vi.fn()}
          onChanged={vi.fn()}
          onFocusLost={onFocusLost}
        />,
      );
    };

    again(other);
    expect(onFocusLost).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Actions for gold" }));
    await user.click(screen.getByRole("menuitem", { name: "New namespace…" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    again(EMPTY);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onFocusLost).toHaveBeenCalledTimes(1);
  });

  it("returns the focus to Actions on Cancel after a refusal", async () => {
    fakeServer({
      ...manageRoutes(),
      "DELETE /api/v1/environments/prod/namespaces/silver": {
        status: 409,
        body: { type: "ConflictError", detail: "silver is not empty; drop what it holds first." },
      },
    });
    const { user } = renderMenu(EMPTY);
    const button = screen.getByRole("button", { name: "Actions for silver" });

    await user.click(button);
    await user.click(screen.getByRole("menuitem", { name: "Delete namespace…" }));
    await user.click(await screen.findByRole("button", { name: "Delete the namespace" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "silver is not empty; drop what it holds first.",
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(button).toHaveFocus();
    });
  });
});
