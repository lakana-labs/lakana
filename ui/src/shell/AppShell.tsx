/**
 * The frame around every page: skip link, sidebar, sign-in notices, the working area, the
 * command palette, the keyboard shortcuts and the tour.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { RouterProvider } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Outlet, useHref, useLocation, useNavigate } from "react-router";

import type { AccessOut } from "../api";
import { Notices } from "../sign-in/Notices";
import { TourProvider } from "../tour/TourProvider";
import { type AreaAccess, visibleAreas } from "./areas";
import { CommandPalette } from "./CommandPalette";
import { InterfaceStateProvider } from "./InterfaceState";
import { MeContext } from "./me";
import { Sidebar } from "./Sidebar";

/**
 * Resolves a link's address for React Aria's links: one inside the interface through the router,
 * one to another site (the documentation) as it is.
 *
 * @param href - The address.
 * @returns The address to put on the link.
 */
function useShellHref(href: string): string {
  const resolved = useHref(href);
  return /^https?:\/\//.test(href) ? href : resolved;
}

const ShortcutsDialog = lazy(async () => {
  const { ShortcutsDialog: loaded } = await import("./ShortcutsDialog");
  return { default: loaded };
});

/** Properties of {@link AppShell}. */
export interface AppShellProps {
  /** The areas the user may use; the others are hidden. */
  readonly access: AreaAccess;
  /** What the person may do, for the screens that change with it. */
  readonly me: AccessOut;
}

/**
 * Renders the shell. Ctrl+K and ⌘ K open the palette from anywhere, a text field included;
 * after a navigation to another path, focus moves to the new page's heading (not when only the
 * query changes, so a search field or a tree keeps it, and not on the first load, so the
 * skip link stays the first stop). The person's access and interface state are there for every
 * screen inside; the interface state is read once, when the shell appears.
 *
 * @param props - The access set and the person.
 * @param props.access - The areas the user may use.
 * @param props.me - What the person may do.
 * @returns The shell with the current page in its working area.
 */
export function AppShell({ access, me }: AppShellProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const main = useRef<HTMLElement>(null);
  const shownPath = useRef(location.pathname);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const openShortcuts = useCallback(() => {
    setShortcutsOpen(true);
  }, []);
  const areas = visibleAreas(access);

  useEffect(() => {
    if (location.pathname !== shownPath.current) {
      shownPath.current = location.pathname;
      main.current?.querySelector("h1")?.focus();
    }
  }, [location.pathname]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.shiftKey &&
        !event.altKey &&
        event.key.toLowerCase() === "k"
      ) {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKeyDown, { capture: true });
    return () => {
      window.removeEventListener("keydown", onKeyDown, { capture: true });
    };
  }, []);

  return (
    <MeContext value={me}>
      <InterfaceStateProvider name={me.name}>
        <RouterProvider
          navigate={(path) => {
            void navigate(path);
          }}
          useHref={useShellHref}
        >
          <TourProvider areas={areas}>
            <div className="shell">
              <a
                className="skip-link"
                href="#main"
                onClick={(event) => {
                  event.preventDefault();
                  main.current?.focus();
                }}
              >
                {t("shell.skip")}
              </a>
              <Sidebar
                areas={areas}
                onSearch={() => {
                  setPaletteOpen(true);
                }}
                onShortcuts={openShortcuts}
              />
              <main className="shell-main" id="main" ref={main} tabIndex={-1}>
                <Notices />
                <Outlet />
              </main>
              <CommandPalette
                areas={areas}
                isOpen={paletteOpen}
                onOpenChange={setPaletteOpen}
                onShortcuts={openShortcuts}
              />
              {shortcutsOpen && (
                <Suspense fallback={null}>
                  <ShortcutsDialog
                    onClose={() => {
                      setShortcutsOpen(false);
                    }}
                  />
                </Suspense>
              )}
            </div>
          </TourProvider>
        </RouterProvider>
      </InterfaceStateProvider>
    </MeContext>
  );
}
