/**
 * The breadcrumb above a detail page's heading: "Access › People › test1".
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

/** One part of a breadcrumb before the page itself. */
export interface Crumb {
  /** Where the part leads. */
  readonly to: string;
  /** The part's words. */
  readonly label: string;
}

/** Properties of {@link Breadcrumbs}. */
export interface BreadcrumbsProps {
  /** The parts before the page, from the area down. */
  readonly trail: readonly Crumb[];
  /** The page itself: the last part, not a link. */
  readonly current: string;
}

/**
 * Renders the breadcrumb: a `nav` landmark named "Breadcrumb", each part but the last a link,
 * the last marked as the current page. The separators are hidden from screen readers, which
 * announce the list instead.
 *
 * @param props - The trail and the page.
 * @param props.trail - The parts before the page.
 * @param props.current - The page itself.
 * @returns The breadcrumb.
 */
export function Breadcrumbs({ trail, current }: BreadcrumbsProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <nav className="breadcrumbs" aria-label={t("shell.breadcrumb")}>
      <ol>
        {trail.map((crumb) => (
          <li key={`${crumb.label} ${crumb.to}`}>
            <Link to={crumb.to}>{crumb.label}</Link>
            <span className="breadcrumb-separator" aria-hidden="true">
              ›
            </span>
          </li>
        ))}
        <li>
          <span aria-current="page">{current}</span>
        </li>
      </ol>
    </nav>
  );
}
