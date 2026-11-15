import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { TableOut, TableRef } from "../../api";
import { manageRoutes } from "../../test/manage";
import { fakeServer } from "../../test/server";
import { table } from "../../test/table";
import TableActions from "./TableActions";

const PLACE: TableRef = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const DEFINED_IN = "Defined in analytics/assets/silver/clean.sql — change it in git";

/**
 * Renders the menu for a table on its page, in a router whose location a test can read.
 *
 * @param shown - The table as the page read it.
 * @returns The router and a user-event session.
 */
function renderActions(shown: TableOut) {
  const router = createMemoryRouter(
    [
      {
        path: "/catalog/prod/gold.sales/fact_sales",
        element: <TableActions place={PLACE} table={shown} />,
      },
      { path: "/catalog", element: <p>The catalog</p> },
    ],
    { initialEntries: ["/catalog/prod/gold.sales/fact_sales"] },
  );
  const user = userEvent.setup();
  const rendered = render(<RouterProvider router={router} />);
  return { router, user, ...rendered };
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

describe("TableActions", () => {
  it("offers exactly the items actions lists, in the order Maintain now, Retention, Rebuild from git, Drop", async () => {
    const { user } = renderActions(
      table({ actions: ["drop", "rebuild", "maintain", "set_retention"] }),
    );

    await user.click(screen.getByRole("button", { name: "Actions" }));

    expect(itemNames()).toEqual(["Maintain now…", "Retention…", "Rebuild from git…", "Drop…"]);
    expect(within(screen.getByRole("menu")).getByRole("separator")).toBeInTheDocument();
  });

  it("offers only what actions lists, without a separator for one group", async () => {
    const { user } = renderActions(table({ actions: ["drop"] }));

    await user.click(screen.getByRole("button", { name: "Actions" }));

    expect(itemNames()).toEqual(["Drop…"]);
    expect(within(screen.getByRole("menu")).queryByRole("separator")).not.toBeInTheDocument();
  });

  it("renders nothing without actions, and nothing for an artifact", () => {
    const { container, unmount } = renderActions(table({ actions: [] }));
    expect(container).toBeEmptyDOMElement();
    unmount();

    const artifact = renderActions(table({ kind: "ARTIFACT", actions: ["drop"] }));
    expect(artifact.container).toBeEmptyDOMElement();
  });

  it("disables a git-owned Drop and says Defined in analytics/assets/silver/clean.sql — change it in git", async () => {
    const { user } = renderActions(
      table({
        actions: ["drop", "rebuild"],
        defined_by: { repository: "analytics", path: "assets/silver/clean.sql" },
      }),
    );

    await user.click(screen.getByRole("button", { name: "Actions" }));

    const menu = screen.getByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Drop…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(within(menu).getByRole("menuitem", { name: "Rebuild from git…" })).not.toHaveAttribute(
      "aria-disabled",
    );
    expect(menu).toHaveAccessibleDescription(DEFINED_IN);
  });

  it("says nothing of git when no project defines the table", async () => {
    const { user } = renderActions(table({ actions: ["drop"] }));

    await user.click(screen.getByRole("button", { name: "Actions" }));

    expect(screen.getByRole("menu")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByRole("menuitem", { name: "Drop…" })).not.toHaveAttribute("aria-disabled");
  });

  it("shows the defining file through VisibleText", async () => {
    const { user } = renderActions(
      table({
        actions: ["drop"],
        defined_by: { repository: "analytics", path: "assets/‮loc.sql" },
      }),
    );

    await user.click(screen.getByRole("button", { name: "Actions" }));

    const mark = screen.getByText("U+202E");
    expect(mark).toHaveClass("char-mark");
    expect(screen.getByRole("menu")).toHaveAccessibleDescription(
      "Defined in analytics/assets/U+202Eloc.sql — change it in git",
    );
  });

  it("opens and closes the menu by keyboard; Escape on a dialog returns the focus to Actions", async () => {
    fakeServer(manageRoutes());
    const { user } = renderActions(table({ actions: ["rebuild", "drop"] }));
    const button = screen.getByRole("button", { name: "Actions" });

    await user.tab();
    expect(button).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
      // React Aria gives the focus back to the trigger on the next frame.
      expect(button).toHaveFocus();
    });

    await user.keyboard("{ArrowDown}");
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await user.keyboard("{ArrowDown}{Enter}");
    expect(await screen.findByRole("dialog")).toHaveAccessibleName("Drop gold.sales.fact_sales?");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(button).toHaveFocus();
  });

  it("goes to the Dropped view with the dropped table in the state after a drop", async () => {
    const server = fakeServer(manageRoutes());
    const { router, user } = renderActions(table({ actions: ["drop"] }));

    await user.click(screen.getByRole("button", { name: "Actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Drop…" }));
    await user.click(await screen.findByRole("button", { name: "Drop the table" }));

    await waitFor(() => {
      expect(router.state.location.pathname + router.state.location.search).toBe(
        "/catalog?env=prod&ns=gold.sales&view=dropped",
      );
    });
    expect(router.state.location.state).toEqual({ dropped: "gold.sales.fact_sales" });
    expect(server.sent.filter((request) => request.method === "DELETE")).toHaveLength(1);
  });

  it("opens Rebuild from git and Cancel returns the focus to Actions", async () => {
    fakeServer(manageRoutes());
    const { user } = renderActions(table({ actions: ["rebuild"] }));
    const button = screen.getByRole("button", { name: "Actions" });

    await user.click(button);
    await user.click(screen.getByRole("menuitem", { name: "Rebuild from git…" }));
    expect(await screen.findByRole("dialog")).toHaveAccessibleName(
      "Rebuild gold.sales.fact_sales from git?",
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(button).toHaveFocus();
  });
});
