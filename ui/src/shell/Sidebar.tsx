/**
 * The sidebar: the product name, Search, the areas in words and the user menu.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Link } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router";

import type { ShippedArea } from "./areas";
import { shortcutLabel } from "./shortcut";
import { UserMenu } from "./UserMenu";

/** Properties of {@link Sidebar}. */
export interface SidebarProps {
  /** The areas the user sees, in sidebar order. */
  readonly areas: readonly ShippedArea[];
  /** Opens the command palette. */
  readonly onSearch: () => void;
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
 * Renders the sidebar. The current area is marked with `aria-current="page"`, an accent bar
 * and a heavier weight, never by colour alone.
 *
 * @param props - The areas and the search action.
 * @param props.areas - The areas the user sees.
 * @param props.onSearch - Opens the command palette.
 * @returns The sidebar as the page's banner.
 */
export function Sidebar({ areas, onSearch }: SidebarProps): JSX.Element {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const groups = [
    areas.filter((area) => area.group === "work"),
    areas.filter((area) => area.group === "admin"),
  ].filter((group) => group.length > 0);

  return (
    <header className="sidebar">
      <p className="sidebar-product">{t("shell.product")}</p>
      {/* A native button: React Aria's Button drops aria-keyshortcuts. */}
      <button
        className="sidebar-search"
        type="button"
        onClick={onSearch}
        aria-keyshortcuts="Control+K Meta+K"
      >
        <span>{t("shell.search")}</span>
        <kbd aria-hidden="true">{t(`shell.searchShortcut.${shortcutLabel(navigator)}`)}</kbd>
      </button>
      <nav className="sidebar-nav">
        {groups.map((group) => (
          <ul className="sidebar-list" key={group[0]?.group}>
            {group.map((area) => (
              <li key={area.id}>
                <Link
                  className="sidebar-link"
                  href={area.path}
                  aria-current={isCurrent(area, pathname) ? "page" : false}
                >
                  {t(`areas.${area.id}.name`)}
                </Link>
              </li>
            ))}
          </ul>
        ))}
      </nav>
      <UserMenu />
    </header>
  );
}
