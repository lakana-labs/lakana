import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import type { AccessOut } from "../api";
import { MeContext } from "../shell/me";
import { GroupEntry } from "./GroupEntry";
import { placeAt } from "./places";

initI18n();

/**
 * Renders the Access group's address for a person, with a stand-in for the page it opens.
 *
 * @param admin - Whether the person is an administrator.
 * @returns The router, for the address it ends on.
 */
function renderAccess(admin: boolean): ReturnType<typeof createMemoryRouter> {
  const group = placeAt("/settings/access");
  if (group === null) throw new Error("no Access group");
  const me: AccessOut = { name: "kim", admin, noAccessYet: false, mayDeploy: false, picture: null };
  const router = createMemoryRouter(
    [
      { path: "/settings/access", element: <GroupEntry group={group} /> },
      { path: "/settings/access/people", element: <h1>People page</h1> },
    ],
    { initialEntries: ["/", "/settings/access"], initialIndex: 1 },
  );
  render(
    <MeContext value={me}>
      <RouterProvider router={router} />
    </MeContext>,
  );
  return router;
}

describe("GroupEntry", () => {
  it("opens the group's first listed place in place of the group's address", async () => {
    const router = renderAccess(true);

    expect(await screen.findByRole("heading", { name: "People page" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/access/people");
    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("says the group is not the person's to use when none of its places is listed", () => {
    renderAccess(false);

    expect(screen.getByRole("heading", { level: 1, name: "You cannot use Access" })).toBeVisible();
  });
});
