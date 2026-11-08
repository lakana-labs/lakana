/**
 * Renders the whole shell at a path in a memory router, for tests.
 *
 * @packageDocumentation
 */
import { render } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";

import { PreferencesProvider } from "../preferences/PreferencesProvider";
import { ALL_SHIPPED, type AreaAccess } from "../shell/areas";
import { appRoutes } from "../shell/routes";
import { MemoryStorage } from "./storage";

/** What {@link renderShell} hands back. */
export interface RenderedShell {
  /** The memory router, to read its location. */
  readonly router: ReturnType<typeof createMemoryRouter>;
  /** The storage the preferences are kept in. */
  readonly storage: Storage;
  /** A user-event session. */
  readonly user: UserEvent;
}

/** Options of {@link renderShell}. */
export interface ShellOptions {
  /** The areas the user may use; every shipped one by default. */
  readonly access?: AreaAccess;
  /** The storage to keep preferences in; an empty in-memory one by default. */
  readonly storage?: Storage;
}

/**
 * Renders the shell at a path.
 *
 * @param path - The URL path to start at.
 * @param options - The access set and the storage.
 * @returns The router, the storage and a user-event session.
 */
export function renderShell(path = "/", options: ShellOptions = {}): RenderedShell {
  const storage = options.storage ?? new MemoryStorage();
  const router = createMemoryRouter(appRoutes(options.access ?? ALL_SHIPPED), {
    initialEntries: [path],
  });
  const user = userEvent.setup();
  render(
    <PreferencesProvider storage={storage}>
      <RouterProvider router={router} />
    </PreferencesProvider>,
  );
  return { router, storage, user };
}
