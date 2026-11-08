/**
 * The routes of the interface: every area is a URL.
 *
 * @packageDocumentation
 */
import type { RouteObject } from "react-router";

import { AppShell } from "./AppShell";
import { type AreaAccess, SHIPPED_AREAS } from "./areas";
import { AreaPage } from "./AreaPage";
import { NotAvailable } from "./NotAvailable";
import { NotFound } from "./NotFound";

/**
 * The route tree: one route per shipped area under the shell, and the not-found state for
 * every other path. A shipped area outside the user's access answers with the permission
 * state, so a deep link to it says why instead of "not found".
 *
 * @param access - The areas the user may use.
 * @returns The routes for `createBrowserRouter` or `createMemoryRouter`.
 */
export function appRoutes(access: AreaAccess): RouteObject[] {
  const areas: RouteObject[] = SHIPPED_AREAS.map((area) => {
    const element = access.has(area.id) ? <AreaPage area={area} /> : <NotAvailable area={area} />;
    return area.path === "/" ? { index: true, element } : { path: area.path.slice(1), element };
  });
  return [
    {
      path: "/",
      element: <AppShell access={access} />,
      children: [...areas, { path: "*", element: <NotFound /> }],
    },
  ];
}
