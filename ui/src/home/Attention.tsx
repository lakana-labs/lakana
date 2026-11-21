/**
 * Home's Needs your attention: what failed, is late or waits for the person, as a dashboard
 * panel of the newest five rows that each open the failure, and a link on to every failed run.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { type AttentionItemOut, type AttentionKind, getAttention } from "../api";
import { runsTabPath } from "../jobs/paths";
import { Loaded } from "../shell/Loaded";
import { useWhen } from "../shell/useWhen";
import { StatusGlyph } from "../status/StatusGlyph";
import { PILL_TONES } from "../status/StatusLabel";
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

/** Properties of {@link State}. */
interface StateProps {
  /** What the row reports. */
  readonly kind: AttentionKind;
}

/**
 * A row's state as a small pill: the glyph of its kind and the kind in words.
 *
 * @param props - The kind.
 * @param props.kind - What the row reports.
 * @returns The pill.
 */
function State({ kind }: StateProps): JSX.Element {
  const { t } = useTranslation();
  const glyph = GLYPHS[kind];
  return (
    <span className={`status-label pill pill-${PILL_TONES[glyph]}`}>
      <StatusGlyph kind={glyph} size={12} />
      <span>{t(`home.attention.kinds.${kind}`)}</span>
    </span>
  );
}

/** Properties of {@link Rows}. */
interface RowsProps {
  /** Every row the server sent, newest first. */
  readonly items: readonly AttentionItemOut[];
  /** Whether the server left more out. */
  readonly truncated: boolean;
}

/**
 * The newest five rows, each opening its failure; under them, when there are more, Show all N
 * on to the Runs tab filtered to failed runs.
 *
 * @param props - The rows and whether more were left out.
 * @param props.items - The rows.
 * @param props.truncated - Whether more were left out.
 * @returns The table and its foot.
 */
function Rows({ items, truncated }: RowsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const since = useSince();
  return (
    <>
      <ObjectTable column={t("list.state")}>
        {items.slice(0, SHOWN).map((item) => (
          <ObjectRow
            key={`${item.kind} ${item.ref} ${item.title}`}
            to={attentionPath(item)}
            name={item.title}
            environment={item.environment}
            second={
              item.count > 1 && item.since !== null
                ? t("home.attention.repeated", { count: item.count, since: since(item.since) })
                : when(item.at)
            }
            aside={<State kind={item.kind} />}
          />
        ))}
      </ObjectTable>
      {items.length > SHOWN && (
        <p className="panel-foot">
          <Link to={runsTabPath({ status: ["failed"] })}>
            {t(truncated ? "home.attention.showAllMore" : "home.attention.showAll", {
              count: items.length,
            })}
          </Link>
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
  return (
    <section className="flat-panel dashboard-panel home-attention" aria-labelledby={heading}>
      <div className="panel-head">
        <div>
          <h2 className="panel-title" id={heading}>
            {t("home.attention.title")}
          </h2>
          <p className="panel-subtitle">{t("home.attention.subtitle", { count: SHOWN })}</p>
        </div>
      </div>
      <Loaded resource={attention}>
        {(found) =>
          found.items.length === 0 ? (
            <p className="empty">{t("home.attention.none")}</p>
          ) : (
            <Rows items={found.items} truncated={found.truncated} />
          )
        }
      </Loaded>
    </section>
  );
}
