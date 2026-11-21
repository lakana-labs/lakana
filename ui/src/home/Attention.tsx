/**
 * Home's Needs your attention: what failed, is late or waits for the person, as a dashboard
 * panel of the newest five rows that each open the failure, and Show all N for the rest.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { type AttentionItemOut, type AttentionKind, getAttention } from "../api";
import { runsTabPath } from "../jobs/paths";
import { Loaded } from "../shell/Loaded";
import { useWhen } from "../shell/useWhen";
import { StatusLabel } from "../status/StatusLabel";
import type { StatusKind } from "../status/status";
import { attentionPath } from "./links";
import { ObjectRow, ObjectTable } from "./ObjectRows";
import { usePolled } from "./usePolled";

/** How often the list is read again: once a minute. */
const EVERY_MS = 60_000;

/** The most rows the panel shows; the rest are one link away. */
const SHOWN = 5;

/** The glyph of each kind: failed for what broke, warning for what waits or is late. */
const GLYPHS: Record<AttentionKind, StatusKind> = {
  run_failed: "failed",
  deploy_failed: "failed",
  table_late: "warning",
  check_failed: "failed",
  query_failed: "failed",
  recovery_link_open: "warning",
  worker_lost: "failed",
};

/** The days of the week a failure in a row is named by: within six days, so never ambiguous. */
const WEEKDAY_DAYS = 6;

/**
 * Whether two moments fall on the same day of the person's clock.
 *
 * @param one - A moment.
 * @param other - Another.
 * @returns True on the same day.
 */
function sameDay(one: Date, other: Date): boolean {
  return one.toDateString() === other.toDateString();
}

/**
 * A function that names when a failure in a row began: the time when it was today, the day of
 * the week within the last six days ("Tuesday"), else the date.
 *
 * @returns The function.
 */
function useSince(): (iso: string) => string {
  const { i18n } = useTranslation();
  const time = new Intl.DateTimeFormat(i18n.language, { timeStyle: "short" });
  const weekday = new Intl.DateTimeFormat(i18n.language, { weekday: "long" });
  const date = new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium" });
  return (iso) => {
    const at = new Date(iso);
    const now = new Date();
    if (sameDay(at, now)) {
      return time.format(at);
    }
    const days = new Date(now);
    days.setDate(days.getDate() - WEEKDAY_DAYS);
    days.setHours(0, 0, 0, 0);
    return at >= days ? weekday.format(at) : date.format(at);
  };
}

/** Properties of {@link Rows}. */
interface RowsProps {
  /** Every row the server sent, newest first. */
  readonly items: readonly AttentionItemOut[];
  /** Whether the server left more out. */
  readonly truncated: boolean;
  /** Whether every row is shown, not only the newest five. */
  readonly expanded: boolean;
  /** Shows every row, or only the newest five again. */
  readonly setExpanded: (expanded: boolean) => void;
}

/**
 * The newest five rows, each opening its failure; under them, when there are more, Show all N
 * shows the rest in place and moves the focus to the first of them (its link, or the row itself
 * when it opens nothing), and Show fewer hides them again with the focus left on the button.
 * When the server cut the list, a muted line under the expanded rows says so and links on to
 * every failed run. A list that drops to five rows or fewer starts collapsed again.
 *
 * @param props - The rows, whether more were left out, and whether all are shown.
 * @param props.items - The rows.
 * @param props.truncated - Whether more were left out.
 * @param props.expanded - Whether every row is shown.
 * @param props.setExpanded - Shows every row or only the newest five.
 * @returns The table and its foot.
 */
function Rows({ items, truncated, expanded, setExpanded }: RowsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const since = useSince();
  const table = useId();
  const opened = useRef(false);
  const more = items.length > SHOWN;
  useEffect(() => {
    if (!more && expanded) {
      setExpanded(false);
    }
  }, [more, expanded, setExpanded]);
  useEffect(() => {
    if (!opened.current) {
      return;
    }
    opened.current = false;
    const first = document.getElementById(table)?.querySelectorAll<HTMLElement>("tbody tr")[SHOWN];
    (first?.querySelector<HTMLElement>("a") ?? first)?.focus();
  }, [expanded, table]);
  const shown = expanded ? items : items.slice(0, SHOWN);
  return (
    <>
      <div
        className="list-scroll"
        role="region"
        aria-label={t("home.attention.region")}
        // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
      >
        <ObjectTable column={t("list.state")} id={table}>
          {shown.map((item) => {
            const to = attentionPath(item);
            return (
              <ObjectRow
                key={`${item.kind} ${item.ref} ${item.title}`}
                to={to}
                name={item.title}
                environment={item.environment}
                second={
                  item.count > 1 && item.since !== null
                    ? t("home.attention.repeated", { count: item.count, since: since(item.since) })
                    : when(item.at)
                }
                aside={
                  <StatusLabel
                    kind={GLYPHS[item.kind]}
                    label={t(`home.attention.kinds.${item.kind}`)}
                  />
                }
                focusable={to === null}
              />
            );
          })}
        </ObjectTable>
      </div>
      {expanded && truncated && (
        <p className="panel-note">
          {t("home.attention.truncated", { count: items.length })}{" "}
          <Link to={runsTabPath({ status: ["failed"] })}>{t("home.attention.everyFailedRun")}</Link>
        </p>
      )}
      {more && (
        <p className="panel-foot">
          <button
            className="link-button"
            type="button"
            aria-expanded={expanded}
            aria-controls={table}
            onClick={() => {
              opened.current = !expanded;
              setExpanded(!expanded);
            }}
          >
            {expanded
              ? t("home.attention.showFewer")
              : t(truncated ? "home.attention.showAllMore" : "home.attention.showAll", {
                  count: items.length,
                })}
          </button>
        </p>
      )}
    </>
  );
}

/**
 * Shows what needs the person, newest first, five at most, and reads it again every minute in
 * the background.
 *
 * @returns The panel.
 */
export function Attention(): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  const attention = usePolled(getAttention, EVERY_MS);
  const [expanded, setExpanded] = useState(false);
  return (
    <section className="flat-panel dashboard-panel home-attention" aria-labelledby={heading}>
      <div className="panel-head">
        <div>
          <h2 className="panel-title" id={heading}>
            {t("home.attention.title")}
          </h2>
          <p className="panel-subtitle">
            {expanded
              ? t("home.attention.subtitleAll")
              : t("home.attention.subtitle", { count: SHOWN })}
          </p>
        </div>
      </div>
      <Loaded resource={attention}>
        {(found) =>
          found.items.length === 0 ? (
            <p className="empty">{t("home.attention.none")}</p>
          ) : (
            <Rows
              items={found.items}
              truncated={found.truncated}
              expanded={expanded}
              setExpanded={setExpanded}
            />
          )
        }
      </Loaded>
    </section>
  );
}
