import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import type { AccessOut } from "../api";
import { MeContext } from "../shell/me";
import { GroupEntry } from "./GroupEntry";
import { placeAt, SETTINGS_PLACES, type SettingsPlace } from "./places";

initI18n();

/**
 * Renders the Access group's address for a person, with a stand-in for the page it opens.
 *
 * @param admin - Whether the person is an administrator.
 * @param places - The registry, when not the real one.
 * @returns The router, for the address it ends on.
 */
function renderAccess(
  admin: boolean,
  places?: readonly SettingsPlace[],
): ReturnType<typeof createMemoryRouter> {
  const group = placeAt("/settings/access");
  if (group === null) throw new Error("no Access group");
  const me: AccessOut = { name: "kim", admin, noAccessYet: false, mayDeploy: false, picture: null };
  const router = createMemoryRouter(
    [
      {
        path: "/settings/access",
        element:
          places === undefined ? (
            <GroupEntry group={group} />
          ) : (
            <GroupEntry group={group} places={places} />
          ),
      },
      { path: "/settings/access/people", element: <h1>People page</h1> },
      { path: "/settings/access/what-i-may-do", element: <h1>What I may do page</h1> },
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

  it("opens What I may do for a person who is not an administrator", async () => {
    const router = renderAccess(false);

    expect(await screen.findByRole("heading", { name: "What I may do page" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/access/what-i-may-do");
  });

  it("says the group is not the person's to use when none of its places is listed", () => {
    renderAccess(
      false,
      SETTINGS_PLACES.filter((place) => place.id !== "what-i-may-do"),
    );

    expect(screen.getByRole("heading", { level: 1, name: "You cannot use Access" })).toBeVisible();
  });
});
