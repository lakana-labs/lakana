/**
 * The routes of the interface: every area is a URL.
 *
 * @packageDocumentation
 */
import type { ComponentType } from "react";
import type { RouteObject } from "react-router";

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
 * The screens of the areas that have one; none yet, every area shows its "coming in part N"
 * page.
 */
const AREA_PAGES: AreaPages = {};

/**
 * The route of one area, without its path.
 *
 * @param area - The area.
 * @param access - The areas the user may use.
 * @param pages - The screens loaded on demand.
 * @param noAccessYet - Whether Home says that the person has no access yet.
 * @returns The element, or the on-demand loader of the area's screen.
 */
function areaRoute(
  area: ShippedArea,
  access: AreaAccess,
  pages: AreaPages,
  noAccessYet: boolean,
): Pick<RouteObject, "element" | "hydrateFallbackElement" | "lazy"> {
  if (!access.has(area.id)) {
    return { element: <NotAvailable area={area} /> };
  }
  const load = pages[area.id];
  if (load === undefined) {
    return { element: <AreaPage area={area} noAccessYet={noAccessYet && area.id === "home"} /> };
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
 * The route tree: one route per shipped area under the shell, and the not-found state for
 * every other path. A shipped area outside the user's access answers with the permission
 * state, so a deep link to it says why instead of "not found", and its screen is never loaded.
 *
 * @param access - The areas the user may use.
 * @param pages - The screens loaded on demand; the interface's own by default.
 * @param noAccessYet - Whether Home says that the person has no access yet.
 * @returns The routes for `createBrowserRouter` or `createMemoryRouter`.
 */
export function appRoutes(
  access: AreaAccess,
  pages: AreaPages = AREA_PAGES,
  noAccessYet = false,
): RouteObject[] {
  const areas = SHIPPED_AREAS.map((area): RouteObject => {
    const route = areaRoute(area, access, pages, noAccessYet);
    return area.path === "/" ? { index: true, ...route } : { path: area.path.slice(1), ...route };
  });
  return [
    {
      path: "/",
      element: <AppShell access={access} />,
      children: [...areas, { path: "*", element: <NotFound /> }],
    },
  ];
}
