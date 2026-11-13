/**
 * The sidebar: the mark and the product name, Search, the areas in words, Help and the user menu.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router";

import type { ShippedArea } from "./areas";
import { HelpMenu } from "./HelpMenu";
import { LakanaMark } from "./LakanaMark";
import { shortcutLabel } from "./shortcut";
import { UserMenu } from "./UserMenu";

/** Properties of {@link Sidebar}. */
export interface SidebarProps {
  /** The areas the user sees, in sidebar order. */
  readonly areas: readonly ShippedArea[];
  /** Opens the command palette. */
  readonly onSearch: () => void;
  /** Opens the keyboard shortcuts (Help). */
  readonly onShortcuts: () => void;
}

/**
 * Whether an area is the one the path is in.
 *
 * @param area - The area.
 * @param pathname - The current path.
 * @returns True when the path is the area's or below it.
 */
function isCurrent(area: ShippedArea, pathname: string): boolean {
  if (area.path === "/") {
    return pathname === "/";
  }
  return pathname === area.path || pathname.startsWith(`${area.path}/`);
}

/**
 * Renders the sidebar. Below 640 px it folds into a bar with the product name and a Menu button
 * that shows the same search, area list, Help and user menu (WCAG 1.4.10, reflow at 320 px). The current area is marked with `aria-current="page"`, an accent bar
 * and a heavier weight, never by colour alone.
 *
 * @param props - The areas, the search action and the shortcuts action.
 * @param props.areas - The areas the user sees.
 * @param props.onSearch - Opens the command palette.
 * @param props.onShortcuts - Opens the keyboard shortcuts.
 * @returns The sidebar as the page's banner.
 */
export function Sidebar({ areas, onSearch, onShortcuts }: SidebarProps): JSX.Element {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const groups = [
    areas.filter((area) => area.group === "work"),
    areas.filter((area) => area.group === "admin"),
  ].filter((group) => group.length > 0);

  const panel = useId();
  const toggle = useRef<HTMLButtonElement>(null);
  // The narrow menu is open for the page it was opened on, so choosing an area closes it.
  const [openOn, setOpenOn] = useState<string | null>(null);
  const open = openOn === pathname;

  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        setOpenOn(null);
        toggle.current?.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <header className="sidebar">
      <div className="sidebar-bar">
        {/* The word names the product, so the mark beside it is decorative (read once, not twice). */}
        <p className="sidebar-product">
          <LakanaMark />
          {t("shell.product")}
        </p>
        {/* Shown only on narrow screens (shell.css), where the panel below is folded away. */}
        <button
          className="sidebar-toggle"
          type="button"
          ref={toggle}
          aria-expanded={open}
          aria-controls={panel}
          onClick={() => {
            setOpenOn(open ? null : pathname);
          }}
        >
          {t("shell.menu")}
        </button>
      </div>
      <div className="sidebar-panel" id={panel} data-open={open}>
        {/* A native button: React Aria's Button drops aria-keyshortcuts. */}
        <button
          className="sidebar-search"
          type="button"
          onClick={onSearch}
          aria-keyshortcuts="Control+K Meta+K"
          data-tour="search"
        >
          <span>{t("shell.search")}</span>
          <kbd aria-hidden="true">{t(`shell.searchShortcut.${shortcutLabel(navigator)}`)}</kbd>
        </button>
        <nav className="sidebar-nav" aria-label={t("shell.areas")}>
          {groups.map((group) => (
            <ul className="sidebar-list" key={group[0]?.group}>
              {group.map((area) => (
                <li key={area.id}>
                  <Link
                    className="sidebar-link"
                    data-tour={area.id}
                    href={area.path}
                    routerOptions={{ state: { restore: true } }}
                    aria-current={isCurrent(area, pathname) ? "page" : false}
                  >
                    {t(`areas.${area.id}.name`)}
                  </Link>
                </li>
              ))}
            </ul>
          ))}
        </nav>
        <HelpMenu onShortcuts={onShortcuts} />
        <UserMenu />
      </div>
    </header>
  );
}
