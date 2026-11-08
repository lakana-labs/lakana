/**
 * The row of tabs under an area's heading: each tab is a place with its own URL.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { NavLink } from "react-router";

/** One tab. */
interface SectionTab {
  /** The tab's URL path. */
  readonly to: string;
  /** The tab's name. */
  readonly label: string;
}

/** Properties of {@link SectionTabs}. */
export interface SectionTabsProps {
  /** The name of the row, for screen readers. */
  readonly label: string;
  /** The tabs, in order; the first one is the area's own path. */
  readonly tabs: readonly SectionTab[];
}

/**
 * Renders the tabs as links; the current one carries `aria-current="page"`.
 *
 * @param props - The row's name and the tabs.
 * @param props.label - The name of the row.
 * @param props.tabs - The tabs.
 * @returns The navigation.
 */
export function SectionTabs({ label, tabs }: SectionTabsProps): JSX.Element {
  return (
    <nav className="tabs" aria-label={label}>
      {tabs.map((tab) => (
        <NavLink key={tab.to} className="tab" to={tab.to} end>
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}
