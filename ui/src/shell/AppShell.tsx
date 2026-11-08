/**
 * The frame around every page: skip link, sidebar, sign-in notices, the working area and the
 * command palette.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { RouterProvider } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Outlet, useHref, useLocation, useNavigate } from "react-router";

import { Notices } from "../sign-in/Notices";
import { type AreaAccess, visibleAreas } from "./areas";
import { CommandPalette } from "./CommandPalette";
import { Sidebar } from "./Sidebar";

/** Properties of {@link AppShell}. */
export interface AppShellProps {
  /** The areas the user may use; the others are hidden. */
  readonly access: AreaAccess;
}

/**
 * Renders the shell. Ctrl+K and ⌘ K open the palette from anywhere, a text field included;
 * after a navigation, focus moves to the new page's heading (not on the first load, so the
 * skip link stays the first stop).
 *
 * @param props - The access set.
 * @param props.access - The areas the user may use.
 * @returns The shell with the current page in its working area.
 */
export function AppShell({ access }: AppShellProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const main = useRef<HTMLElement>(null);
  const firstKey = useRef(location.key);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const areas = visibleAreas(access);

  useEffect(() => {
    if (location.key !== firstKey.current) {
      main.current?.querySelector("h1")?.focus();
    }
  }, [location.key]);

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
    <RouterProvider
      navigate={(path) => {
        void navigate(path);
      }}
      useHref={useHref}
    >
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
        />
        <main className="shell-main" id="main" ref={main} tabIndex={-1}>
          <Notices />
          <Outlet />
        </main>
        <CommandPalette areas={areas} isOpen={paletteOpen} onOpenChange={setPaletteOpen} />
      </div>
    </RouterProvider>
  );
}
