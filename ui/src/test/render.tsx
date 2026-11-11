/**
 * Renders the whole shell at a path in a memory router, signed in, for tests.
 *
 * @packageDocumentation
 */
import { render } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";

import type { AccessOut } from "../api";
import { PreferencesProvider } from "../preferences/PreferencesProvider";
import { ALL_SHIPPED, type AreaAccess } from "../shell/areas";
import { type AreaPages, appRoutes } from "../shell/routes";
import { SessionContext, type SessionValue } from "../sign-in/SessionProvider";
import { homeRoutes } from "./home";
import { type FakeRoute, type FakeServer, fakeServer } from "./server";
import { signedIn } from "./session";
import { MemoryStorage } from "./storage";

/** What {@link renderShell} hands back. */
export interface RenderedShell {
  /** The memory router, to read its location. */
  readonly router: ReturnType<typeof createMemoryRouter>;
  /** The storage the preferences are kept in. */
  readonly storage: Storage;
  /** A user-event session. */
  readonly user: UserEvent;
  /** The fake server the shell talks to. */
  readonly server: FakeServer;
}

/** Options of {@link renderShell}. */
export interface ShellOptions {
  /** The areas the user may use; every shipped one by default. */
  readonly access?: AreaAccess;
  /** The storage to keep preferences in; an empty in-memory one by default. */
  readonly storage?: Storage;
  /** Area pages loaded on demand; none by default. */
  readonly pages?: AreaPages;
  /** What the person may do; an administrator named `admin`, with access, by default. */
  readonly me?: Partial<AccessOut>;
  /** The session; a signed-in administrator by default. */
  readonly session?: SessionValue;
  /** Server routes beyond the default (no sign-in notices). */
  readonly routes?: Record<string, FakeRoute>;
}

/**
 * Renders the shell at a path.
 *
 * @param path - The URL path to start at.
 * @param options - The access set, the storage, the session, what the person may do and the
 *   server's routes (Home's and the interface state's answer "nothing to report" by default).
 * @returns The router, the storage, a user-event session and the fake server.
 */
export function renderShell(path = "/", options: ShellOptions = {}): RenderedShell {
  const storage = options.storage ?? new MemoryStorage();
  const me: AccessOut = {
    name: "admin",
    admin: true,
    mayDeploy: true,
    picture: null,
    noAccessYet: false,
    ...options.me,
  };
  const server = fakeServer({
    "GET /api/v1/sign-in/notices": { status: 200, body: [] },
    ...homeRoutes(me.name),
    ...options.routes,
  });
  const router = createMemoryRouter(appRoutes(options.access ?? ALL_SHIPPED, options.pages, me), {
    initialEntries: [path],
  });
  const user = userEvent.setup();
  render(
    <PreferencesProvider storage={storage}>
      <SessionContext value={options.session ?? signedIn()}>
        <RouterProvider router={router} />
      </SessionContext>
    </PreferencesProvider>,
  );
  return { router, storage, user, server };
}
