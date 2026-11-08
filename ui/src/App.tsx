/**
 * The root component of the interface: the router inside the preferences.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { createBrowserRouter, RouterProvider } from "react-router";

import { PreferencesProvider } from "./preferences/PreferencesProvider";
import { ALL_SHIPPED } from "./shell/areas";
import { appRoutes } from "./shell/routes";

// Until sign-in (part 1b), every shipped area is visible: none of them reads data yet.
const router = createBrowserRouter(appRoutes(ALL_SHIPPED));

/**
 * The whole interface.
 *
 * @returns The router with every area as a URL.
 */
export function App(): JSX.Element {
  return (
    <PreferencesProvider>
      <RouterProvider router={router} />
    </PreferencesProvider>
  );
}
