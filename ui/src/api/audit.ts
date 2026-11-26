/**
 * The audit log as Settings › Audit log reads it: a page of events at a time, newest first.
 *
 * @remarks
 * Its own module, loaded only with the page. The server hides what the reader may not see (the
 * query text and column values of other people's events without `READ_QUERY_TEXT`) and records
 * each read that shows someone else's events as `ACTIVITY_VIEWED`, as for `lakana audit list`.
 * @packageDocumentation
 */
import { api } from "./client";
import { type CursorPage, withQuery } from "./manage";
import { type ListSortChoice, setSort } from "./sorting";

/** How many events a page holds. */
export const AUDIT_PAGE = 50;

/** One audit event, as `GET /api/v1/audit-events` answers it. */
export interface AuditEventOut {
  /** Its id. */
  readonly id: string;
  /** When it happened (ISO 8601). */
  readonly occurred_at: string;
  /** Whom it is attributed to; null for nobody (a failed sign-in). */
  readonly principal_id: string | null;
  /** That principal's name; null for nobody. */
  readonly principal_name: string | null;
  /** Its type; `UNKNOWN` for a type a newer release wrote. */
  readonly event_type: string;
  /** Its type as stored: the newer release's name of an `UNKNOWN` one. */
  readonly event_type_raw: string;
  /** The level of the object it is about, or null. */
  readonly securable_kind: "INSTANCE" | "ENVIRONMENT" | "NAMESPACE" | "TABLE" | null;
  /** The object's environment, or null. */
  readonly securable_environment: string | null;
  /** The object's dotted namespace, or null. */
  readonly securable_namespace: string | null;
  /** The object's table, or null. */
  readonly securable_table: string | null;
  /** Its fields, with what the reader may not see already hidden by the server. */
  readonly detail: Readonly<Record<string, unknown>>;
}

/** What the log is filtered by; null is no narrowing. */
export interface AuditFilter {
  /** The name of the principal the events are attributed to. */
  readonly principal: string | null;
  /** The event type, such as `GRANT_CREATED`. */
  readonly eventType: string | null;
  /** The environment of the events' object. */
  readonly environment: string | null;
  /** The earliest time (ISO 8601). */
  readonly since: string | null;
}

/** Every event. */
export const NO_AUDIT_FILTER: AuditFilter = {
  principal: null,
  eventType: null,
  environment: null,
  since: null,
};

/**
 * Reads one page of the audit log.
 *
 * @param filter - The filter, sent with every page.
 * @param sort - The order; null for the newest first.
 * @param cursor - The previous page's cursor; null for the first.
 * @returns The events and the cursor of the next page.
 * @throws {ApiError} for a refusal: 403 without `READ_AUDIT_LOG`, 422 for a filter the server
 *   refuses, 503 when the read could not be recorded.
 */
export async function listAuditEvents(
  filter: AuditFilter,
  sort: ListSortChoice | null,
  cursor: string | null,
): Promise<CursorPage<AuditEventOut>> {
  const query = new URLSearchParams({ limit: String(AUDIT_PAGE) });
  const values: Record<string, string | null> = {
    principal_name: filter.principal,
    event_type: filter.eventType,
    environment: filter.environment,
    since: filter.since,
  };
  for (const [name, value] of Object.entries(values)) {
    if (value !== null) query.set(name, value);
  }
  setSort(query, sort);
  if (cursor !== null) query.set("cursor", cursor);
  const body = await api.request<{
    items: readonly AuditEventOut[];
    next_cursor: string | null;
  }>("GET", withQuery("/api/v1/audit-events", query));
  return { items: body.items, nextCursor: body.next_cursor };
}
