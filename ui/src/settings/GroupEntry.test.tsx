import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import { GroupEntry } from "./GroupEntry";
import { placeAt, SETTINGS_PLACES, type SettingsPlace, type SettingsViewer } from "./places";
import { SettingsViewerContext } from "./viewer";

initI18n();

/**
 * Renders the Access group's address for a person, with a stand-in for the page it opens.
 *
 * @param admin - Whether the person is an administrator.
 * @param places - The registry, when not the real one.
 * @param secretEnvironments - Where the person manages secrets; null while not read yet.
 * @returns The router, for the address it ends on, and a way to render the viewer anew.
 */
function renderAccess(
  admin: boolean,
  places?: readonly SettingsPlace[],
  secretEnvironments: readonly string[] | null = [],
): ReturnType<typeof createMemoryRouter> & {
  readonly learn: (environments: readonly string[]) => void;
} {
  const group = placeAt("/settings/access");
  if (group === null) throw new Error("no Access group");
  const viewer = (known: readonly string[] | null): SettingsViewer => ({
    admin,
    noAccessYet: false,
    secretEnvironments: known,
    managePolicy: admin,
    readsEnvironments: admin,
    instanceActions: new Set(),
    managedEnvironments: [],
  });
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
      { path: "/settings/access/secrets", element: <h1>Secrets page</h1> },
      { path: "/settings/access/what-i-may-do", element: <h1>What I may do page</h1> },
    ],
    { initialEntries: ["/", "/settings/access"], initialIndex: 1 },
  );
  const { rerender } = render(
    <SettingsViewerContext value={viewer(secretEnvironments)}>
      <RouterProvider router={router} />
    </SettingsViewerContext>,
  );
  return Object.assign(router, {
    learn: (environments: readonly string[]) => {
      rerender(
        <SettingsViewerContext value={viewer(environments)}>
          <RouterProvider router={router} />
        </SettingsViewerContext>,
      );
    },
  });
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

  it("waits for a pending place before the first listed one", async () => {
    const router = renderAccess(false, undefined, null);

    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/access");
    router.learn(["dev"]);

    expect(await screen.findByRole("heading", { name: "Secrets page" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings/access/secrets");
  });

  it("says the group is not the person's to use when none of its places is listed", () => {
    renderAccess(
      false,
      SETTINGS_PLACES.filter((place) => place.id !== "what-i-may-do"),
    );

    expect(screen.getByRole("heading", { level: 1, name: "You cannot use Access" })).toBeVisible();
  });
});
