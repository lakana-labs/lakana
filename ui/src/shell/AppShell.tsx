/**
 * The frame around every page: skip link, sidebar, sign-in notices, the working area, the
 * command palette, the keyboard shortcuts and the tour.
 *
 * @packageDocumentation
 */
import {
  type JSX,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { RouterProvider } from "react-aria-components";
import { useTranslation } from "react-i18next";
import {
  NavigationType,
  Outlet,
  type NavigateOptions,
  useHref,
  useLocation,
  useNavigate,
  useNavigationType,
} from "react-router";

declare module "react-aria-components" {
  /** What a React Aria link may hand to the router: the options of React Router's `navigate`. */
  interface RouterConfig {
    /** The options of `navigate`, such as the state of the new history entry. */
    routerOptions: NavigateOptions;
  }
}

import type { AccessOut } from "../api";
import { Notices } from "../sign-in/Notices";
import { TourProvider } from "../tour/TourProvider";
import { type AreaAccess, visibleAreas } from "./areas";
import { CommandPalette } from "./CommandPalette";
import { InterfaceStateProvider } from "./InterfaceState";
import { MeContext } from "./me";
import { overlayOpen } from "./overlay";
import { isRedirect } from "./redirect";
import { Sidebar } from "./Sidebar";
import { TruncationTip } from "./TruncationTip";

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
 * Whether a key press lands where text is typed: an input, a text area, a select or editable
 * content in any of its forms (`contenteditable`, `="true"`, `="plaintext-only"`; the SQL
 * editor's, inside its shadow root), but not inside an element marked `contenteditable="false"`.
 *
 * @param target - The innermost element of the key press.
 * @returns True when it does.
 */
function isTextEntry(target: EventTarget | undefined): boolean {
  if (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement
  ) {
    return true;
  }
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  // The nearest element that says whether its content is editable decides, as the browser does.
  const nearest = target.closest("[contenteditable]");
  return nearest !== null && nearest.getAttribute("contenteditable") !== "false";
}

/**
 * Renders the shell. Ctrl+K and ⌘ K open the palette from anywhere, a text field included;
 * after a navigation to another path, focus moves to the new page's heading (not when only the
 * query changes, so a search field or a tree keeps it, not after a push whose history state is
 * `{ keepFocus: true }`, so a control that moved to its own new address (Run) keeps it, and not
 * on the first load, so the skip link stays the first stop). The first load includes the
 * redirects it lands on: a Replace whose state carries the redirect mark (`REDIRECT`, in
 * `./redirect`) made before any other navigation. Every other Replace is a move like a Push,
 * such as a list that replaces a deleted object's page. The move happens in a layout effect, in
 * the commit that shows the new page, so it comes before the page's own effects: a page that
 * puts focus somewhere on arrival (a note on what just happened, as `PlaceNote`) does so in an
 * ordinary effect and wins, without waiting a frame. The person's access and interface
 * state are there for every screen inside; the interface state is read once, when the shell
 * appears.
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

  const navigationType = useNavigationType();
  // Only the move that asked for it: Back and Forward bring the entry's state back too, and
  // they move to the heading like any other page.
  const keepFocus =
    navigationType === NavigationType.Push &&
    (location.state as { keepFocus?: unknown } | null)?.keepFocus === true;
  // A redirect the first load lands on (/settings to its first place) is the page opening, not
  // a move: the skip link stays the first stop. A redirect after a move still focuses.
  const moved = useRef(false);
  const redirect = navigationType === NavigationType.Replace && isRedirect(location.state);
  // A layout effect, so focus moves in the commit that shows the new page: a passive effect
  // runs in a later task, and until then the page is on screen with focus left on the link
  // that was pressed (or on nothing), where a key press would go.
  useLayoutEffect(() => {
    if (location.pathname !== shownPath.current) {
      shownPath.current = location.pathname;
      const firstRedirect = redirect && !moved.current;
      moved.current ||= !redirect;
      if (!keepFocus && !firstRedirect) {
        main.current?.querySelector("h1")?.focus();
      }
    }
  }, [location.pathname, keepFocus, redirect]);

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

  // Alt+Left goes back from any page, as in the browser, which a headless or embedded browser may
  // not do on its own; doing it here also makes it one behaviour everywhere. A widget that used the
  // key itself keeps it (defaultPrevented: this listener bubbles, after theirs). While a dialog, a
  // menu or a popover is open it does nothing, a text field inside one included, and the browser
  // is stopped too, so the page behind the overlay is never left. Otherwise a text field keeps it
  // (it moves the cursor there on a Mac) and the browser does what it does there.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      if (
        event.defaultPrevented ||
        event.key !== "ArrowLeft" ||
        !event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey
      ) {
        return;
      }
      if (overlayOpen()) {
        event.preventDefault();
        return;
      }
      if (isTextEntry(event.composedPath()[0])) {
        return;
      }
      event.preventDefault();
      void navigate(-1);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [navigate]);

  return (
    <MeContext value={me}>
      <InterfaceStateProvider name={me.name}>
        <RouterProvider
          navigate={(path, options) => {
            void navigate(path, options);
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
              <TruncationTip />
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
