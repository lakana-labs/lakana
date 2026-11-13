/**
 * The Health tab: every check with its latest result, every signal with what it observed, the
 * last maintenance, and who read the table in the last 7 days. Its code loads only when the tab
 * is opened.
 *
 * @remarks
 * Only an administrator asks for the readers' names (`readers=true`); the server records that
 * read in the audit log and refuses it with 503 when it cannot. Everyone else asks without them
 * and sees the count the table already carries.
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router";

import { ApiError, getTableHealth, type TableHealthOut, type TableRef } from "../../api";
import { personPath } from "../../access/personPath";
import { CopyLine } from "../../shell/CopyLine";
import { shellQuote } from "../../shell/shellQuote";
import { useMe } from "../../shell/me";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { useResource } from "../../shell/useResource";
import { VisibleText } from "../../shell/VisibleText";
import { StatusGlyph } from "../../status/StatusGlyph";
import type { StatusKind } from "../../status/status";
import { ReadFailed, When } from "../cells";
import { formatBytes } from "../format";
import { firedLines, judged } from "./health";
import { useTableData } from "./TableContext";

type Check = TableHealthOut["checks"][number];
type Signal = TableHealthOut["signals"][number];

/** The glyph of each check outcome the server sends. */
const OUTCOME_GLYPHS: Readonly<Record<string, StatusKind>> = {
  passed: "succeeded",
  failed: "failed",
  errored: "failed",
  warned: "warning",
};

const SIGNAL_NAMES = ["freshness", "volume", "null_rate", "range", "schema"];
const SIGNAL_STATES = ["ok", "fired", "no_history", "no_metrics", "off"];
const SIGNAL_EFFECTS = ["warn", "block", "off"];

/**
 * A word the interface has for a server value, else the server's value with its invisible
 * characters marked.
 *
 * @param known - The values the interface has a word for.
 * @param value - The server's value.
 * @param word - The word for a known value.
 * @returns The word or the value.
 */
function wordOr(
  known: readonly string[],
  value: string,
  word: (value: string) => string,
): ReactNode {
  return known.includes(value) ? word(value) : <VisibleText text={value} />;
}

/**
 * An observed value or a band as `key value` pairs, each value as the server sent it.
 *
 * @param values - The object.
 * @returns The pairs, separated by commas.
 */
function pairs(values: Readonly<Record<string, unknown>>): string {
  return Object.entries(values)
    .map(([key, value]) => `${key} ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
}

/** Properties of the tab's sections. */
interface HealthProps {
  /** The health the server answered. */
  readonly health: TableHealthOut;
}

/**
 * Every check with its result, failing rows and when it finished, the count of reference checks
 * hidden from the person, and the CLI command for the history.
 *
 * @param props - The health.
 * @param props.health - The health the server answered.
 * @returns The section.
 */
function Checks({ health }: HealthProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const { ref } = useTableData();
  const row = (check: Check): JSX.Element => {
    const glyph = OUTCOME_GLYPHS[check.outcome];
    return (
      <tr key={check.name}>
        <td>
          <VisibleText text={check.name} />
        </td>
        <td>
          <span className="glyph-word">
            {glyph !== undefined && <StatusGlyph kind={glyph} />}
            {wordOr(Object.keys(OUTCOME_GLYPHS), check.outcome, (outcome) =>
              t(`table.healthTab.checks.outcome.${outcome}`),
            )}
          </span>
          {check.error !== null && (
            <span className="second">
              <VisibleText text={check.error} />
            </span>
          )}
        </td>
        <td className="number">
          {check.failing_rows === null
            ? t("table.healthTab.checks.notCounted")
            : new Intl.NumberFormat(i18n.language).format(check.failing_rows)}
        </td>
        <td>
          <When iso={check.finished_at} never="" />
        </td>
      </tr>
    );
  };
  return (
    <section className="health-section">
      <h2 className="block-heading">{t("table.healthTab.checks.heading")}</h2>
      {health.checks.length === 0 ? (
        <p className="second">{t("table.healthTab.checks.none")}</p>
      ) : (
        <table className="list list-static">
          <thead>
            <tr>
              <th scope="col">{t("table.healthTab.checks.name")}</th>
              <th scope="col">{t("table.healthTab.checks.result")}</th>
              <th scope="col" className="number">
                {t("table.healthTab.checks.failingRows")}
              </th>
              <th scope="col">{t("table.healthTab.checks.finished")}</th>
            </tr>
          </thead>
          <tbody>{health.checks.map(row)}</tbody>
        </table>
      )}
      {health.hidden_checks > 0 && (
        <p className="second">
          {t("table.healthTab.checks.hidden", { count: health.hidden_checks })}
        </p>
      )}
      <p className="second">{t("table.healthTab.checks.history")}</p>
      <CopyLine
        text={`lakana check list --env ${shellQuote(ref.environment)} --table ${shellQuote(`${ref.namespace}.${ref.name}`)}`}
        copyLabel={t("table.healthTab.checks.copy")}
      />
    </section>
  );
}

/**
 * One signal's row: what it watches, its state, what it observed against its usual band, its
 * effect and when it last fired. Values a mask or a filter hides stay hidden.
 *
 * @param props - The signal.
 * @param props.signal - The signal's current state.
 * @returns The row.
 */
function SignalRow({ signal }: { readonly signal: Signal }): JSX.Element {
  const { t } = useTranslation();
  const hidden = t("table.healthTab.signals.hidden");
  const fired = signal.state === "fired";
  return (
    <tr>
      <td>
        {wordOr(SIGNAL_NAMES, signal.signal, (name) => t(`table.healthTab.signals.names.${name}`))}
      </td>
      <td>
        {signal.column === null ? (
          t("table.healthTab.signals.wholeTable")
        ) : (
          <VisibleText text={signal.column} />
        )}
      </td>
      <td>
        <span className="glyph-word">
          {fired && <StatusGlyph kind={signal.effect === "block" ? "failed" : "warning"} />}
          {signal.state === "ok" && <StatusGlyph kind="succeeded" />}
          {wordOr(SIGNAL_STATES, signal.state, (state) =>
            t(`table.healthTab.signals.states.${state}`),
          )}
        </span>
      </td>
      <td>
        {signal.values_hidden ? (
          hidden
        ) : signal.observed === null ? (
          t("table.healthTab.signals.nothing")
        ) : (
          <VisibleText text={pairs(signal.observed)} />
        )}
      </td>
      <td>
        {signal.values_hidden ? (
          hidden
        ) : signal.band === null ? (
          t("table.healthTab.signals.noBand")
        ) : (
          <VisibleText text={pairs(signal.band)} />
        )}
      </td>
      <td>
        {wordOr(SIGNAL_EFFECTS, signal.effect, (effect) =>
          t(`table.healthTab.signals.effects.${effect}`),
        )}
      </td>
      <td>
        <When iso={signal.last_fired_at ?? null} never={t("table.healthTab.signals.neverFired")} />
      </td>
    </tr>
  );
}

/**
 * Every signal in a region that scrolls on its own, and under it the sentence of each one that
 * fired, as the Overview words it.
 *
 * @param props - The health.
 * @param props.health - The health the server answered.
 * @returns The section.
 */
function Signals({ health }: HealthProps): JSX.Element {
  const { t } = useTranslation();
  const lines = firedLines({ ...health, checks: [] }, t);
  const columns = ["signal", "column", "state", "observed", "band", "effect", "lastFired"];
  return (
    <section className="health-section">
      <h2 className="block-heading">{t("table.healthTab.signals.heading")}</h2>
      {health.signals.length === 0 ? (
        <p className="second">{t("table.healthTab.signals.none")}</p>
      ) : (
        <>
          <div
            className="list-scroll"
            role="region"
            aria-label={t("table.healthTab.signals.region")}
            // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
            tabIndex={0}
          >
            <table className="list list-static">
              <thead>
                <tr>
                  {columns.map((column) => (
                    <th key={column} scope="col">
                      {t(`table.healthTab.signals.${column}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {health.signals.map((signal) => (
                  <SignalRow key={signal.id} signal={signal} />
                ))}
              </tbody>
            </table>
          </div>
          {lines.length > 0 && (
            <ul className="health-lines">
              {lines.map((line, index) => (
                <li key={String(index)}>
                  <StatusGlyph kind={line.severity === "failing" ? "failed" : "warning"} />
                  <span>{line.sentence}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

/**
 * The last maintenance that changed the table: when, whether it completed, and what it removed.
 *
 * @param props - The health.
 * @param props.health - The health the server answered.
 * @returns The section.
 */
function Maintenance({ health }: HealthProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const last = health.last_maintenance;
  const fact = (term: string, answer: ReactNode): JSX.Element => (
    <div className="fact">
      <dt>{t(`table.healthTab.maintenance.${term}`)}</dt>
      <dd>{answer}</dd>
    </div>
  );
  return (
    <section className="health-section">
      <h2 className="block-heading">{t("table.healthTab.maintenance.heading")}</h2>
      {last === null ? (
        <p className="second">{t("table.healthTab.maintenance.none")}</p>
      ) : (
        <dl className="maintenance">
          {fact("finished", <When iso={last.finished_at} never="" />)}
          {fact("status", t(`table.healthTab.maintenance.statuses.${last.status}`))}
          {fact("snapshots", String(last.snapshots_expired))}
          {fact(
            "files",
            t("table.healthTab.maintenance.filesSize", {
              count: last.files_deleted,
              size: formatBytes(last.bytes_deleted, i18n.language),
            }),
          )}
          {fact("tags", String(last.tags_removed))}
        </dl>
      )}
    </section>
  );
}

/**
 * Who read the table in the last 7 days. An administrator sees each name, linked to the person,
 * with the reads and the last read; everyone else, and an administrator the server sent no
 * names, sees only how many people read it. When the names were refused because the read could
 * not be recorded, a line says so with the next step. Opened at `#readers`, the heading takes the
 * focus.
 *
 * @param props - The health and whether the names were refused.
 * @param props.health - The health the server answered.
 * @param props.refused - Whether the names were refused because the read could not be audited.
 * @returns The section.
 */
function Readers({ health, refused }: HealthProps & { readonly refused: boolean }): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const { table } = useTableData();
  const { hash } = useLocation();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (hash === "#readers") {
      heading.current?.focus();
    }
  }, [hash]);
  // Only an administrator asked for the names; whatever else arrives is not shown.
  const readers = me.admin ? health.readers : null;
  let shown: ReactNode;
  if (readers === null) {
    shown = (
      <>
        {refused && <p role="status">{t("table.healthTab.readers.unavailable")}</p>}
        <p>{t("table.healthTab.readers.count", { count: table.readers_7d })}</p>
      </>
    );
  } else if (readers.length === 0) {
    shown = <p className="second">{t("table.healthTab.readers.none")}</p>;
  } else {
    shown = (
      <>
        <table className="list list-static">
          <thead>
            <tr>
              <th scope="col">{t("table.healthTab.readers.person")}</th>
              <th scope="col" className="number">
                {t("table.healthTab.readers.reads")}
              </th>
              <th scope="col">{t("table.healthTab.readers.lastRead")}</th>
            </tr>
          </thead>
          <tbody>
            {readers.map((reader) => (
              <tr key={reader.principal}>
                <td>
                  <Link to={personPath(reader.principal)}>
                    <VisibleText text={reader.principal} />
                  </Link>
                </td>
                <td className="number">
                  {t("table.healthTab.readers.readCount", { count: reader.read_count })}
                </td>
                <td>
                  <When iso={reader.last_read_at} never="" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="second">{t("table.healthTab.readers.audited")}</p>
      </>
    );
  }
  return (
    <section className="health-section" id="readers">
      <h2 className="block-heading" tabIndex={-1} ref={heading}>
        {t("table.healthTab.readers.heading")}
      </h2>
      {shown}
    </section>
  );
}

/** What the tab read: the health, and whether the readers' names were refused. */
interface HealthRead {
  /** The health the server answered. */
  readonly health: TableHealthOut;
  /** Whether an administrator's read with the names was refused because it could not be audited. */
  readonly readersRefused: boolean;
}

/**
 * Reads the health: for an administrator with the readers' names, and when the server cannot
 * record that read (503) once more without them, so checks and signals still show; for everyone
 * else without them.
 *
 * @param ref - The table.
 * @param admin - Whether the person is an administrator.
 * @returns The health, and whether the names were refused.
 * @throws {ApiError} for any other refusal.
 */
async function readHealth(ref: TableRef, admin: boolean): Promise<HealthRead> {
  if (admin) {
    try {
      return { health: await getTableHealth(ref, { readers: true }), readersRefused: false };
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 503)) {
        throw error;
      }
    }
  }
  return { health: await getTableHealth(ref, { readers: false }), readersRefused: admin };
}

/**
 * The Health tab. It reads the health with the readers' names for an administrator and without
 * them for everyone else. A failed read, the audit's 503 included, says why with Try again; the
 * focus then goes to the tab, because the button is gone.
 *
 * @returns The tab's content.
 */
export default function HealthTab(): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const { ref } = useTableData();
  const { environment, namespace, name } = ref;
  const load = useCallback(
    () => readHealth({ environment, namespace, name }, me.admin),
    [environment, namespace, name, me.admin],
  );
  const resource = useResource(load);
  const { target, arm } = useFocusAfter<HTMLDivElement>(resource.data);
  const read = resource.data;
  let shown: ReactNode;
  if (resource.error !== null) {
    shown = (
      <ReadFailed
        message={resource.error}
        onRetry={() => {
          arm();
          resource.reload();
        }}
      />
    );
  } else if (read === undefined) {
    shown = <p role="status">{t("table.healthTab.reading")}</p>;
  } else {
    const { health } = read;
    shown = (
      <>
        {!judged(health) && <p>{t("table.healthTab.notJudged")}</p>}
        <Checks health={health} />
        <Signals health={health} />
        <Maintenance health={health} />
        <Readers health={health} refused={read.readersRefused} />
      </>
    );
  }
  return (
    <div className="health-tab" ref={target} tabIndex={-1}>
      {shown}
    </div>
  );
}
