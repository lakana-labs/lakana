/**
 * The interface as a whole: the sign-in pages at `/sign-in`, and every area behind the session.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  createPath,
  type DataRouter,
  Navigate,
  Outlet,
  type RouteObject,
  RouterProvider,
  useLocation,
} from "react-router";

import { type AccessOut, myAccess } from "../api";
import { areasFor } from "../shell/areas";
import { type AreaPages, appRoutes } from "../shell/routes";
import { AddPasskey } from "./AddPasskey";
import { type LinkFragment, takeLinkFragment } from "./fragment";
import { LinkLanding } from "./LinkLanding";
import { SessionProvider, type SessionValue, useSession } from "./SessionProvider";
import { SignInPage } from "./SignInPage";

/**
 * Makes the router for a route tree: `createBrowserRouter` in the browser. The interface makes
 * a new one when the person's areas change, at the location the old one showed.
 */
export type RouterFactory = (routes: RouteObject[], at: string | undefined) => DataRouter;

/** Access for a person whose rights cannot be read: the administration areas stay hidden. */
const NO_KNOWN_RIGHTS: AccessOut = { admin: false, noAccessYet: false };

/**
 * The path a page that sent the browser to sign-in left in the history state.
 *
 * @param state - The location's state.
 * @returns The path, or Home.
 */
function cameFrom(state: unknown): string {
  const from = (state as { readonly from?: unknown } | null)?.from;
  return typeof from === "string" ? from : "/";
}

/**
 * Says the interface is finding out where it is, in words.
 *
 * @returns The status.
 */
function Loading(): JSX.Element {
  const { t } = useTranslation();
  return (
    <main className="step">
      <p className="step-status" role="status">
        {t("session.loading")}
      </p>
    </main>
  );
}

/** Properties of {@link SignInRoute}. */
interface SignInRouteProps {
  /** The sign-in link taken from the address, or null. */
  readonly link: LinkFragment | null;
  /** Leaves the link behind. */
  readonly onLinkDone: () => void;
}

/**
 * `/sign-in`: the link's page while there is a link, else the sign-in page, or for a
 * signed-in browser the page it wanted.
 *
 * @param props - The link.
 * @param props.link - The sign-in link, or null.
 * @param props.onLinkDone - Leaves the link behind.
 * @returns The page.
 */
function SignInRoute({ link, onLinkDone }: SignInRouteProps): JSX.Element {
  const session = useSession();
  const location = useLocation();
  if (link !== null) {
    return <LinkLanding link={link} onDone={onLinkDone} />;
  }
  if (session.state === "signed-in") {
    return <Navigate to={cameFrom(location.state)} replace />;
  }
  return <SignInPage />;
}

/** Properties of {@link Gate}. */
interface GateProps {
  /** True once the person's areas are known. */
  readonly ready: boolean;
}

/**
 * Lets only a signed-in person past: a signed-out browser goes to sign-in (keeping where it
 * wanted to go), a session that needs a passkey sees only the passkey step, and a passkey
 * offer comes before the areas until the person answers it.
 *
 * @param props - Whether the areas are known.
 * @param props.ready - True once the person's areas are known.
 * @returns The step, or the areas.
 */
function Gate({ ready }: GateProps): JSX.Element {
  const session = useSession();
  const location = useLocation();
  if (session.state !== "signed-in" || session.session === undefined) {
    return <Navigate to="/sign-in" replace state={{ from: location.pathname + location.search }} />;
  }
  if (session.session.needs_passkey) {
    return <AddPasskey required account={session.session} />;
  }
  if (session.passkeyOffered) {
    return <AddPasskey required={false} account={session.session} />;
  }
  return ready ? <Outlet /> : <Loading />;
}

/**
 * Reads what the signed-in person may use, once per session.
 *
 * @param session - The session value.
 * @returns The access, or null until it is known.
 */
function useAccess(session: SessionValue): AccessOut | null {
  const signedIn = session.state === "signed-in" ? session.session : undefined;
  const key =
    signedIn === undefined || signedIn.needs_passkey
      ? null
      : `${signedIn.name} ${signedIn.absolute_expires_at}`;
  const [found, setFound] = useState<{ key: string; access: AccessOut } | null>(null);
  useEffect(() => {
    if (key === null) {
      return;
    }
    myAccess().then(
      (access) => {
        setFound({ key, access });
      },
      () => {
        setFound({ key, access: NO_KNOWN_RIGHTS });
      },
    );
  }, [key]);
  return found !== null && found.key === key ? found.access : null;
}

/**
 * Makes routers: each new route tree gets a new router at the location the router on screen
 * shows. Which router is on screen is recorded after React shows it, never during a render.
 */
class RouterHost {
  readonly #create: RouterFactory;
  #shown: DataRouter | null = null;

  /**
   * Builds the host.
   *
   * @param create - Makes a router.
   */
  constructor(create: RouterFactory) {
    this.#create = create;
  }

  /**
   * Makes the router for a new route tree, at the shown router's location.
   *
   * @param routes - The route tree.
   * @returns The new router.
   */
  routerFor(routes: RouteObject[]): DataRouter {
    const at = this.#shown === null ? undefined : createPath(this.#shown.state.location);
    return this.#create(routes, at);
  }

  /**
   * Records the router React now shows.
   *
   * @param router - The router.
   */
  shown(router: DataRouter): void {
    this.#shown = router;
  }
}

/** Properties of {@link Routed}. */
interface RoutedProps {
  /** Makes the router. */
  readonly createRouter: RouterFactory;
  /** The screens loaded on demand. */
  readonly pages: AreaPages | undefined;
  /** The sign-in link taken from the address, or null. */
  readonly link: LinkFragment | null;
  /** Leaves the link behind. */
  readonly onLinkDone: () => void;
}

/**
 * The routes, once the session is known.
 *
 * @param props - The router factory, the pages and the link.
 * @param props.createRouter - Makes the router.
 * @param props.pages - The screens loaded on demand.
 * @param props.link - The sign-in link, or null.
 * @param props.onLinkDone - Leaves the link behind.
 * @returns The router, or the loading state.
 */
function Routed({ createRouter, pages, link, onLinkDone }: RoutedProps): JSX.Element {
  const session = useSession();
  const access = useAccess(session);
  const [host] = useState(() => new RouterHost(createRouter));
  const routes = useMemo(
    (): RouteObject[] => [
      { path: "/sign-in", element: <SignInRoute link={link} onLinkDone={onLinkDone} /> },
      {
        element: <Gate ready={access !== null} />,
        children: appRoutes(areasFor(access?.admin === true), pages, access?.noAccessYet === true),
      },
    ],
    [access, pages, link, onLinkDone],
  );
  const router = useMemo(() => host.routerFor(routes), [host, routes]);
  useEffect(() => {
    host.shown(router);
    // The old router is disposed only once the new one is on screen.
    return () => {
      router.dispose();
    };
  }, [host, router]);
  if (session.state === "loading") {
    return <Loading />;
  }
  return <RouterProvider router={router} />;
}

/** Properties of {@link Interface}. */
export interface InterfaceProps {
  /** Makes the router: `createBrowserRouter` in the browser, a memory router in tests. */
  readonly createRouter: RouterFactory;
  /** The screens loaded on demand; the interface's own by default. */
  readonly pages?: AreaPages;
  /** Where a skipped passkey offer is remembered; the browser's local storage by default. */
  readonly storage?: Storage | null | undefined;
}

/**
 * Renders the interface. A sign-in link in the address is taken out of it first, before the
 * session is read or any other request is sent.
 *
 * @param props - The router factory, the pages and the storage.
 * @param props.createRouter - Makes the router.
 * @param props.pages - The screens loaded on demand.
 * @param props.storage - Where a skipped passkey offer is remembered.
 * @returns The interface.
 */
export function Interface({ createRouter, pages, storage }: InterfaceProps): JSX.Element {
  const [link, setLink] = useState(() => takeLinkFragment(window.location, window.history));
  const onLinkDone = useCallback(() => {
    setLink(null);
  }, []);
  return (
    <SessionProvider storage={storage}>
      <Routed createRouter={createRouter} pages={pages} link={link} onLinkDone={onLinkDone} />
    </SessionProvider>
  );
}
