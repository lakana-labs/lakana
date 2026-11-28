/**
 * Settings › Audit log: who did what, newest first, filtered by who, event, environment and
 * period, for administrators.
 *
 * @remarks
 * The server is the one that hides and records: of another principal's event, a reader without
 * `READ_QUERY_TEXT` gets a query's text as its fingerprint and a withheld value as a marker
 * (`lakana_core/audit/content.py`), and a page that shows someone else's events, or a filter on
 * someone else, writes `ACTIVITY_VIEWED` as `lakana audit list` does. The page only reads; it
 * keeps nothing, and reads again only when asked (Refresh, a filter, the order, Show more).
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";

import { type AuditEventOut, type AuditFilter, listAuditEvents } from "../api/audit";
import { listEnvironments } from "../api/environments";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { ClearFilters } from "../access/ListBits";
import "../access/access.css";
import { personPath } from "../access/personPath";
import { usePrincipalSearch } from "../access/suggest";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { DOCS_URL } from "../shell/docs";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { countWords, ListToolbar, useSteadyMore } from "../shell/ListToolbar";
import type { ListSortSpec } from "../shell/listSort";
import { type NameSearch, NamePicker, PICKER_LIMIT } from "../shell/NamePicker";
import { SortHeader } from "../shell/SortHeader";
import { settingParam, useAddressParams } from "../shell/useAddressParams";
import { useListSort } from "../shell/useListSort";
import { useResource } from "../shell/useResource";
import { VisibleText } from "../shell/VisibleText";
import { type DetailValue, detailFields, eventObject } from "./auditDetail";
import { AUDIT_EVENT_TYPES } from "./auditEventTypes";
import "./auditStrings";
import { SettingsFrame } from "./SettingsFrame";
import "./settingsStrings";

/** Where exporting and keeping the log is described. */
const EXPORT_URL = `${DOCS_URL}how-to/export-and-keep-the-audit-log/`;

/** The one column the log sorts by: when, the newest first unless the address says otherwise. */
const AUDIT_SORTS: ListSortSpec<"occurred" | "event" | "who" | "on"> = {
  fields: { occurred: "desc", event: "asc", who: "asc", on: "asc" },
  natural: { field: "occurred", order: "desc" },
};

/**
 * How far back the log is read, in days, as the Runs tab offers it; `all` has no start. Its own
 * table: importing the Jobs area's would move that module into a chunk the Jobs screen loads too.
 */
const PERIOD_DAYS = { "24h": 1, "7d": 7, "30d": 30, all: null } as const;

/** A choice of the Period menu. */
type Period = keyof typeof PERIOD_DAYS;

/** Every period, in the menu's order. */
const PERIOD_CHOICES = Object.keys(PERIOD_DAYS) as readonly Period[];

/** The period shown when the address names none. */
const DEFAULT_PERIOD: Period = "30d";

/**
 * The time a period starts, counted back from now.
 *
 * @param period - The period.
 * @returns The time (ISO 8601, UTC); null for all time.
 */
function startOf(period: Period): string | null {
  const days = PERIOD_DAYS[period];
  return days === null ? null : new Date(Date.now() - days * 86_400_000).toISOString();
}

/** What the page is filtered by, as the address holds it (`?who=&event=&environment=&period=`). */
interface AuditChoice {
  /** The principal's name; empty for anyone. */
  readonly who: string;
  /** The event type; empty for every one. */
  readonly event: string;
  /** The environment; null for every one. */
  readonly environment: string | null;
  /** How far back the events go. */
  readonly period: Period;
}

/**
 * Reads the filter from the address.
 *
 * @param params - The address's query.
 * @returns The filter.
 */
function readChoice(params: URLSearchParams): AuditChoice {
  return {
    who: params.get("who")?.trim() ?? "",
    event: params.get("event")?.trim() ?? "",
    environment: params.get("environment"),
    period: PERIOD_CHOICES.find((choice) => choice === params.get("period")) ?? DEFAULT_PERIOD,
  };
}

/**
 * The filter the server reads; the period counts back from now, at the moment of the read.
 *
 * @param choice - The page's filter.
 * @returns The route's filter.
 */
function toFilter(choice: AuditChoice): AuditFilter {
  return {
    principal: choice.who === "" ? null : choice.who,
    eventType: choice.event === "" ? null : choice.event,
    environment: choice.environment,
    since: startOf(choice.period),
  };
}

/**
 * The event types that contain a text, in any letter case; a search that asks no server.
 *
 * @param text - What is typed.
 * @returns The first {@link PICKER_LIMIT} matches, and whether there are more.
 */
const eventSearch: NameSearch<null> = (text) => {
  const wanted = text.trim().toUpperCase();
  const found = AUDIT_EVENT_TYPES.filter((type) => type.includes(wanted));
  return Promise.resolve({
    choices: found
      .slice(0, PICKER_LIMIT)
      .map((type) => ({ key: type, name: type, detail: null, data: null })),
    more: found.length > PICKER_LIMIT,
  });
};

/** Properties of {@link AppliedName}. */
interface AppliedNameProps {
  /** The label. */
  readonly label: string;
  /** The name the address holds; empty for none. */
  readonly applied: string;
  /** Asks for suggestions; null for a plain field. */
  readonly search: NameSearch<null> | null;
  /** The sentence for a text nothing matches. */
  readonly noMatch: (text: string) => string;
  /**
   * Checks what is typed before it is applied: the name to apply, or a refusal to show under
   * the field. Every name is applied as typed by default.
   */
  readonly check?: (typed: string) => { readonly name: string } | { readonly refusal: string };
  /** Applies a name; empty for none. */
  readonly onApply: (name: string) => void;
}

/**
 * A name field of the filters: a chosen suggestion applies at once, a typed name on Enter or
 * when the field is left, never one per key. A name the address brings from elsewhere (Back,
 * Clear filters) replaces the one typed.
 *
 * @param props - The label, the name, the search and the handlers.
 * @param props.label - The label.
 * @param props.applied - The name in the address.
 * @param props.search - The search.
 * @param props.noMatch - The no-match sentence.
 * @param props.check - Checks what is typed.
 * @param props.onApply - Applies a name.
 * @returns The field.
 */
function AppliedName({
  label,
  applied,
  search,
  noMatch,
  check = (typed) => ({ name: typed }),
  onApply,
}: AppliedNameProps): JSX.Element {
  const [typed, setTyped] = useState(applied);
  const [refusal, setRefusal] = useState<string | null>(null);
  // The name last read from the address, and the one this field last sent: the address changes
  // a moment after the send, so until then it still holds the old name, which must not set the
  // field back (the list would open again on the text coming back, as in Grants' Who filter).
  const [shownFor, setShownFor] = useState(applied);
  const [sent, setSent] = useState<string | null>(null);
  if (shownFor !== applied) {
    setShownFor(applied);
    setSent(null);
    if (sent !== applied) {
      setTyped(applied);
      setRefusal(null);
    }
  }
  const apply = (text: string): void => {
    const trimmed = text.trim();
    const checked = trimmed === "" ? { name: "" } : check(trimmed);
    if ("refusal" in checked) {
      setRefusal(checked.refusal);
      return;
    }
    setRefusal(null);
    // Only a name the check wrote differently (an event type in capitals) goes back into the
    // field: a late blur that applies the old text must not undo Clear filters.
    if (checked.name !== text) setTyped(checked.name);
    if (checked.name !== applied) {
      setSent(checked.name);
      onApply(checked.name);
    }
  };
  return (
    <NamePicker
      label={label}
      value={typed}
      onChange={(value) => {
        setTyped(value);
        setRefusal(null);
      }}
      search={search}
      noMatch={noMatch}
      error={refusal}
      onPick={(choice) => {
        apply(choice.name);
      }}
      onEnter={() => {
        apply(typed);
      }}
      onBlur={() => {
        apply(typed);
      }}
    />
  );
}

/** Properties of {@link FieldValue}. */
interface FieldValueProps {
  /** The value. */
  readonly value: DetailValue;
}

/**
 * One field's value: the text with its hidden characters marked, or the words for what the
 * server left out or hid.
 *
 * @param props - The value.
 * @param props.value - The value.
 * @returns The value's content.
 */
function FieldValue({ value }: FieldValueProps): JSX.Element {
  const { t } = useTranslation();
  switch (value.kind) {
    case "text":
      return <VisibleText text={value.text} />;
    case "none":
      return <span className="audit-quiet">{t("settings.audit.null")}</span>;
    case "withheld":
      return <span className="audit-quiet">{t("settings.audit.withheld")}</span>;
    case "fingerprint":
      return (
        <span className="audit-quiet">
          {t("settings.audit.fingerprint", {
            sha: value.sha256.slice(0, 12),
            count: value.length,
          })}
        </span>
      );
  }
}

/** Properties of {@link AuditRow}. */
interface AuditRowProps {
  /** The event. */
  readonly event: AuditEventOut;
  /** Formats a time. */
  readonly when: (iso: string) => string;
}

/**
 * One event: when (to the second), its type, who (their page's link), what it was on, and Show
 * details, which opens its fields in a row of their own beneath it, across the list's width.
 *
 * @param props - The event and the time format.
 * @param props.event - The event.
 * @param props.when - Formats a time.
 * @returns The row, and the row of its fields while they are open.
 */
function AuditRow({ event, when }: AuditRowProps): JSX.Element {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const factsId = useId();
  const fields = detailFields(event);
  const object = eventObject(event);
  const type =
    event.event_type === "UNKNOWN"
      ? t("settings.audit.unknown", { type: event.event_type_raw })
      : event.event_type;
  const time = when(event.occurred_at);
  return (
    <>
      <tr className={open ? "audit-row audit-open" : "audit-row"}>
        <td className="audit-when">
          <time dateTime={event.occurred_at}>{time}</time>
        </td>
        <td>
          <span className="audit-type">
            {event.event_type === "UNKNOWN" ? (
              <Trans
                t={t}
                i18nKey="settings.audit.unknownType"
                components={{ type: <VisibleText text={event.event_type_raw} /> }}
              />
            ) : (
              <VisibleText text={type} />
            )}
          </span>
        </td>
        <td>
          {event.principal_name === null ? (
            <span className="audit-quiet">{t("settings.audit.nobody")}</span>
          ) : (
            <Link className="truncate audit-who" to={personPath(event.principal_name)}>
              <VisibleText text={event.principal_name} />
            </Link>
          )}
        </td>
        <td>
          {object === null && <span className="audit-quiet">{t("settings.audit.nothing")}</span>}
          {object === "instance" && t("settings.audit.instance")}
          {object !== null && object !== "instance" && (
            <>
              <VisibleText text={object.environment} />
              {object.rest !== null && (
                <>
                  {" · "}
                  <VisibleText text={object.rest} />
                </>
              )}
            </>
          )}
        </td>
        <td>
          {fields.length === 0 ? (
            <span className="audit-quiet">{t("settings.audit.noDetails")}</span>
          ) : (
            <Button
              className="link-button"
              aria-expanded={open}
              {...(open ? { "aria-controls": factsId } : {})}
              aria-label={t(open ? "settings.audit.hideDetailsOf" : "settings.audit.detailsOf", {
                type,
                when: time,
              })}
              onPress={() => {
                setOpen(!open);
              }}
            >
              {open ? t("settings.audit.hideDetails") : t("settings.audit.showDetails")}
            </Button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="audit-facts-row">
          <td colSpan={5}>
            <dl className="fact-list audit-facts" id={factsId}>
              {fields.map((field) => (
                <div key={field.name} className="audit-fact">
                  <dt>
                    <VisibleText text={field.name} />
                  </dt>
                  <dd>
                    <FieldValue value={field.value} />
                  </dd>
                </div>
              ))}
            </dl>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * The time of an event to the second, in the person's language: the order of a log is in its
 * seconds.
 *
 * @returns The function.
 */
function useSecondsWhen(): (iso: string) => string {
  const { i18n } = useTranslation();
  const format = new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
  return (iso) => format.format(new Date(iso));
}

/**
 * Renders Settings › Audit log: what the log is and what stays hidden, the link to exporting
 * it, the count and Refresh, the filters (in the address; a menu choice one step of the history, a typed name none),
 * and the events, 50 a page with Show more, sorted by When on the server. A filter that matches
 * nothing offers Clear filters. A refusal or a failed read shows the server's sentence with
 * Try again.
 *
 * @returns The place.
 */
export function AuditPage(): JSX.Element {
  const { t } = useTranslation();
  const [params, update] = useAddressParams();
  const choice = readChoice(params);
  const sorting = useListSort(AUDIT_SORTS);
  const sort = sorting.choice;
  const principals = usePrincipalSearch();
  const environments = useResource(listEnvironments);
  const when = useSecondsWhen();
  const { who, event, environment, period } = choice;
  const key = JSON.stringify([who, event, environment, period]);
  const load = useCallback(
    (cursor: string | null) =>
      listAuditEvents(toFilter({ who, event, environment, period }), sort, cursor),
    [who, event, environment, period, sort],
  );
  const pages = useCursorPages(key, load, JSON.stringify(sort), (row: AuditEventOut) => row.id);
  const rows = pages.items ?? [];
  const more = useSteadyMore(pages.hasMore, pages.busy);
  const region = useRef<HTMLDivElement>(null);
  // Show more: the events shown before it. An event's open fields are a row of their own, so the
  // first new event is found by its own rows, not by the list's.
  const moreFrom = useRef<number | null>(null);
  useEffect(() => {
    const from = moreFrom.current;
    if (from === null) return;
    if (rows.length > from) {
      moreFrom.current = null;
      const row = region.current?.querySelectorAll("tbody tr.audit-row")[from];
      (row?.querySelector<HTMLElement>("a[href], button") ?? region.current)?.focus();
    } else if (!pages.busy && !pages.hasMore) {
      moreFrom.current = null;
      region.current?.focus();
    }
  });
  // A menu's choice is a step of the history; a typed name replaces the address, as in Grants.
  const set = (name: string, value: string | null, step: "push" | "replace" = "push"): void => {
    update(settingParam(name, value), { replace: step === "replace" });
  };
  const narrowed = who !== "" || event !== "" || environment !== null || period !== DEFAULT_PERIOD;
  const listed = environments.data ?? [];
  // An environment the address names that is not listed (deleted since) is still a choice.
  const choices =
    environment === null || listed.some((choice) => choice.name === environment)
      ? listed
      : [...listed, { name: environment, protected: false }];
  return (
    <SettingsFrame title={t("settings.places.audit")}>
      <p className="page-purpose">{t("settings.audit.purpose")}</p>
      <p>{t("settings.audit.hidden")}</p>
      <p>
        <a href={EXPORT_URL} target="_blank" rel="noopener noreferrer">
          {t("settings.audit.export")}
        </a>
      </p>
      <ListToolbar
        count={countWords(t, "settings.audit", pages.items?.length, more)}
        onRefresh={pages.reload}
        refreshing={pages.busy}
      />
      <div className="access-filters">
        <AppliedName
          label={t("settings.audit.filters.who")}
          applied={who}
          search={principals}
          noMatch={(text) => t("picker.noPrincipal", { name: text })}
          onApply={(name) => {
            set("who", name, "replace");
          }}
        />
        <AppliedName
          label={t("settings.audit.filters.event")}
          applied={event}
          search={eventSearch}
          noMatch={(text) => t("settings.audit.filters.noEvent", { name: text })}
          check={(typed) => {
            const type = typed.toUpperCase();
            return AUDIT_EVENT_TYPES.includes(type)
              ? { name: type }
              : { refusal: t("settings.audit.filters.noEvent", { name: typed }) };
          }}
          onApply={(type) => {
            set("event", type, "replace");
          }}
        />
        <EnvironmentPicker
          environments={choices}
          value={environment}
          label={t("settings.audit.filters.environment")}
          all={t("settings.audit.filters.allEnvironments")}
          onChange={(chosen: string | null) => {
            set("environment", chosen);
          }}
        />
        <ChoiceMenu
          className="env-picker"
          label={t("settings.audit.filters.period")}
          choices={PERIOD_CHOICES.map((id) => ({
            id,
            text: t(`settings.audit.filters.periods.${id}`),
          }))}
          selected={period}
          onChoose={(chosen) => {
            set("period", chosen === DEFAULT_PERIOD ? null : chosen);
          }}
        />
      </div>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={narrowed ? t("settings.audit.noMatch") : t("settings.audit.empty")}
        region={t("settings.audit.region")}
        scroll="audit-scroll"
        regionRef={region}
        onMore={() => {
          moreFrom.current = rows.length;
        }}
        head={
          <tr>
            <SortHeader {...sorting.header("occurred")}>{t("settings.audit.when")}</SortHeader>
            <SortHeader {...sorting.header("event")}>{t("settings.audit.event")}</SortHeader>
            <SortHeader {...sorting.header("who")}>{t("settings.audit.who")}</SortHeader>
            <SortHeader {...sorting.header("on")}>{t("settings.audit.on")}</SortHeader>
            {/* The details are what each kind of event records, with no one order. */}
            <th scope="col">{t("settings.audit.details")}</th>
          </tr>
        }
      >
        {rows.map((row) => (
          <AuditRow key={row.id} event={row} when={when} />
        ))}
      </ViewList>
      <ClearFilters
        pages={pages}
        filtered={narrowed}
        label={t("settings.audit.clear")}
        onClear={() => {
          update((current) => {
            for (const name of ["who", "event", "environment", "period"]) current.delete(name);
            return current;
          });
        }}
      />
    </SettingsFrame>
  );
}
