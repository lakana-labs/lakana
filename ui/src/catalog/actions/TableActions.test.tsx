import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import type { ArtifactOut, TableOut, TableRef } from "../../api";
import { artifact, artifactRoutes } from "../../test/artifacts";
import { manageRoutes } from "../../test/manage";
import { renderShell } from "../../test/render";
import { fakeServer } from "../../test/server";
import { table } from "../../test/table";
import { fakeWebAuthn } from "../../test/webauthn";
import TableActions from "./TableActions";

const PLACE: TableRef = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const DEFINED_IN = "Defined in analytics/assets/silver/clean.sql — change it in git";
const ARTIFACT_PLACE: TableRef = { environment: "prod", namespace: "ml", name: "model" };
const ARTIFACT_PAGE = "/catalog/prod/ml/model";
const ARTIFACT_DROP = "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model";
const GIT_OWNED_ARTIFACT =
  "`ml.model` is produced by a deployed project. Defined in analytics/assets/ml/model.py. Remove it there and deploy; then drop it from Orphans. To free space now, delete single versions: `lakana artifact versions delete ml.model VERSION --env prod`.";

/**
 * Renders the menu for a table on its page, in a router whose location a test can read.
 *
 * @param shown - The table as the page read it.
 * @param read - The artifact as the page read it; null for a table.
 * @returns The router and a user-event session.
 */
function renderActions(shown: TableOut, read: ArtifactOut | null = null) {
  const router = createMemoryRouter(
    [
      {
        path: "/catalog/prod/gold.sales/fact_sales",
        element: (
          <TableActions
            place={read === null ? PLACE : ARTIFACT_PLACE}
            table={shown}
            artifact={read}
          />
        ),
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

  it("renders nothing without actions", () => {
    const { container } = renderActions(table({ actions: [] }));
    expect(container).toBeEmptyDOMElement();
  });

  it("offers Drop for an artifact when its actions list drop, and nothing otherwise", async () => {
    // The table entry's actions are a table's; the artifact's own decide.
    const entry = table({ kind: "ARTIFACT", actions: ["drop", "rebuild"] });
    const { user, unmount } = renderActions(entry, artifact());

    await user.click(screen.getByRole("button", { name: "Actions" }));
    expect(itemNames()).toEqual(["Drop…"]);
    expect(screen.getByRole("menu")).not.toHaveAttribute("aria-describedby");
    unmount();

    expect(renderActions(entry, artifact({ actions: [] })).container).toBeEmptyDOMElement();
  });

  it("renders nothing for an artifact before its own read arrives", () => {
    const { container } = renderActions(table({ kind: "ARTIFACT", actions: ["drop"] }));
    expect(container).toBeEmptyDOMElement();
  });

  it("disables a git-owned Drop and still offers Delete on its versions", async () => {
    const { user } = renderActions(
      table({ kind: "ARTIFACT", actions: [] }),
      artifact({ defined_by: { repository: "analytics", path: "assets/ml/model.py" } }),
    );

    await user.click(screen.getByRole("button", { name: "Actions" }));

    const menu = screen.getByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Drop…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(menu).toHaveAccessibleDescription(
      "Defined in analytics/assets/ml/model.py — change it in git",
    );
  });

  it("drops an artifact after the passkey touch and lands on its namespace with Dropped artifact ml.model focused", async () => {
    const webauthn = fakeWebAuthn();
    let tries = 0;
    const { router, server, user } = renderShell(ARTIFACT_PAGE, {
      routes: {
        ...artifactRoutes(),
        ...manageRoutes(),
        [ARTIFACT_DROP]: () => {
          tries += 1;
          return tries === 1
            ? {
                status: 403,
                body: { type: "StepUpRequired", detail: "Confirm with your passkey." },
              }
            : { status: 204 };
        },
        "POST /api/v1/sign-in/step-up/options": {
          status: 200,
          body: { ceremony_id: "c-1", public_key: {} },
        },
        "POST /api/v1/sign-in/step-up": { status: 204 },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Drop…" }));
    expect(await screen.findByRole("dialog")).toHaveAccessibleName("Drop ml.model?");
    await user.click(await screen.findByRole("button", { name: "Drop the artifact" }));

    const note = await screen.findByText(
      "Dropped artifact ml.model. Its files are removed from storage, and Lakana's periodic clean-up removes any it could not.",
    );
    await waitFor(() => {
      expect(note).toHaveFocus();
    });
    expect(router.state.location.pathname + router.state.location.search).toBe(
      "/catalog?env=prod&ns=ml",
    );
    expect(router.state.location.state).toEqual({
      placeNote: "artifactDropped",
      namespace: "ml",
      artifact: "ml.model",
    });
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(
      server.sent
        .filter((request) => request.method !== "GET" && !request.path.endsWith("/recent"))
        .map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      ARTIFACT_DROP,
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      ARTIFACT_DROP,
    ]);
  });

  it("shows the server's git-owned refusal word for word and stays", async () => {
    fakeServer({
      ...manageRoutes(),
      [ARTIFACT_DROP]: { status: 409, body: { type: "ConflictError", detail: GIT_OWNED_ARTIFACT } },
    });
    const { router, user } = renderActions(table({ kind: "ARTIFACT", actions: [] }), artifact());

    await user.click(screen.getByRole("button", { name: "Actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Drop…" }));
    await user.click(await screen.findByRole("button", { name: "Drop the artifact" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(GIT_OWNED_ARTIFACT, {
      normalizeWhitespace: false,
    });
    expect(screen.getByRole("alert").textContent).toBe(GIT_OWNED_ARTIFACT);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/catalog/prod/gold.sales/fact_sales");
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

  it("opens Maintain now and Close after queuing returns the focus to Actions", async () => {
    fakeServer(manageRoutes());
    const { user } = renderActions(table({ actions: ["maintain"] }));
    const button = screen.getByRole("button", { name: "Actions" });

    await user.click(button);
    await user.click(screen.getByRole("menuitem", { name: "Maintain now…" }));
    expect(await screen.findByRole("dialog")).toHaveAccessibleName(
      "Maintain gold.sales.fact_sales now",
    );
    await user.click(screen.getByRole("button", { name: "Maintain now" }));
    await user.click(await screen.findByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(button).toHaveFocus();
  });

  it("opens Retention and Cancel returns the focus to Actions", async () => {
    fakeServer(manageRoutes());
    const { user } = renderActions(table({ actions: ["set_retention"] }));
    const button = screen.getByRole("button", { name: "Actions" });

    await user.click(button);
    await user.click(screen.getByRole("menuitem", { name: "Retention…" }));
    expect(await screen.findByRole("dialog")).toHaveAccessibleName(
      "Retention of gold.sales.fact_sales",
    );
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(button).toHaveFocus();
  });
});
