/**
 * The root component of the interface: the browser's router inside the preferences.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { createBrowserRouter } from "react-router";

import { PreferencesProvider } from "./preferences/PreferencesProvider";
import { Interface, type RouterFactory } from "./sign-in/Interface";

/**
 * Makes the browser's router; it reads the location from the address bar itself.
 *
 * @param routes - The route tree.
 * @returns The router.
 */
const browserRouter: RouterFactory = (routes) => createBrowserRouter(routes);

/**
 * The whole interface.
 *
 * @returns The sign-in pages and every area as a URL.
 */
export function App(): JSX.Element {
  return (
    <PreferencesProvider>
      <Interface createRouter={browserRouter} />
    </PreferencesProvider>
  );
}
