/**
 * The routes of the interface: every area is a URL.
 *
 * @packageDocumentation
 */
import type { ComponentType } from "react";
import type { RouteObject } from "react-router";

import type { AccessOut } from "../api";
import { AppShell } from "./AppShell";
import { type AreaAccess, type AreaId, SHIPPED_AREAS, type ShippedArea } from "./areas";
import { AreaPage } from "./AreaPage";
import { NotAvailable } from "./NotAvailable";
import { NotFound } from "./NotFound";

/** A module that holds an area's screen, loaded with `import()`. */
export interface AreaModule {
  /** The area's screen. */
  readonly AreaScreen: ComponentType;
}

/**
 * The areas whose screen is loaded on demand, each by a dynamic `import()`, so its code lands
 * in its own chunk and never in the entry chunk (`ui/.size-limit.mjs` limits each).
 *
 * @example
 * `{ queries: () => import("../queries/QueriesScreen") }`
 */
export type AreaPages = Partial<Record<AreaId, () => Promise<AreaModule>>>;

/**
 * The screens of the areas that have one; every other area shows its "coming in part N" page.
 * A screen holds the area's places under its path (`/access/invites`), so it owns `<area>/*`.
 */
const AREA_PAGES: AreaPages = {
  home: () => import("../home/HomeScreen"),
  catalog: () => import("../catalog/CatalogScreen"),
  access: () => import("../access/AccessScreen"),
  settings: () => import("../settings/SettingsScreen"),
};

/**
 * The route of one area, without its path.
 *
 * @param area - The area.
 * @param access - The areas the user may use.
 * @param pages - The screens loaded on demand.
 * @returns The element, or the on-demand loader of the area's screen.
 */
function areaRoute(
  area: ShippedArea,
  access: AreaAccess,
  pages: AreaPages,
): Pick<RouteObject, "element" | "hydrateFallbackElement" | "lazy"> {
  if (!access.has(area.id)) {
    return { element: <NotAvailable area={area} /> };
  }
  const load = pages[area.id];
  if (load === undefined) {
    return { element: <AreaPage area={area} /> };
  }
  return {
    // A deep link shows the shell with an empty working area until the chunk arrives (React
    // Router needs an element, not null, to notice it); a navigation inside the interface waits
    // for the chunk with the old page still shown.
    hydrateFallbackElement: <></>,
    lazy: async () => {
      const { AreaScreen } = await load();
      return { element: <AreaScreen /> };
    },
  };
}

/**
 * The route tree: one route per shipped area under the shell (with every path under it when the
 * area has a screen), one's own passkeys and sessions at `/account`, and the not-found state for
 * every other path. A shipped area outside the user's access answers with the permission
 * state, so a deep link to it says why instead of "not found", and its screen is never loaded.
 *
 * @param access - The areas the user may use.
 * @param pages - The screens loaded on demand; the interface's own when undefined.
 * @param me - What the person may do; Home and the interface state follow it.
 * @returns The routes for `createBrowserRouter` or `createMemoryRouter`.
 */
export function appRoutes(
  access: AreaAccess,
  pages: AreaPages | undefined,
  me: AccessOut,
): RouteObject[] {
  const screens = pages ?? AREA_PAGES;
  const areas = SHIPPED_AREAS.map((area): RouteObject => {
    const route = areaRoute(area, access, screens);
    if (area.path === "/") {
      return { index: true, ...route };
    }
    const below = screens[area.id] === undefined ? "" : "/*";
    return { path: `${area.path.slice(1)}${below}`, ...route };
  });
  return [
    {
      path: "/",
      element: <AppShell access={access} me={me} />,
      children: [
        ...areas,
        {
          path: "account",
          hydrateFallbackElement: <></>,
          lazy: async () => {
            const { AccountScreen } = await import("../sign-in/AccountScreen");
            return { element: <AccountScreen /> };
          },
        },
        { path: "*", element: <NotFound /> },
      ],
    },
  ];
}
