/**
 * What the server answers Settings › Audit log with in the smoke tests: a page of events of
 * every shape the page shows, filtered by who and event as the route does, and two
 * environments.
 *
 * @packageDocumentation
 */
import type { Page } from "@playwright/test";

/** The paths this file's stubs answer, beside those of `session.ts`. */
export const AUDIT_STUBBED = ["/api/v1/audit-events"];

/** A query text's SHA-256, as another person's shows to a reader without the right. */
export const SQL_SHA256 = "3f7a9c2e5b1d4a6f8e0c2b4d6f8a0c2e4b6d8f0a2c4e6b8d0f2a4c6e8b0d2f4a";

/** A principal name that is long enough to make the table wider than a phone. */
export const LONG_PRINCIPAL = `nightly_loader_${"x".repeat(80)}`;

/**
 * One event.
 *
 * @param id - Its id.
 * @param more - What differs from maria's grant in `dev`.
 * @returns The event.
 */
function anEvent(id: string, more: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id,
    occurred_at: "2026-11-24T09:12:05Z",
    principal_id: "7d3a3b6e-1f0c-4c55-8a3e-2e9f5b1c0a11",
    principal_name: "maria",
    event_type: "GRANT_CREATED",
    event_type_raw: "GRANT_CREATED",
    securable_kind: "ENVIRONMENT",
    securable_environment: "dev",
    securable_namespace: null,
    securable_table: null,
    detail: { grant_id: "5c1e9a7b", principal: "ben", role: "ANALYST" },
    ...more,
  };
}

/** The events, newest first. */
const AUDIT_EVENTS = [
  anEvent("0b6f1c1e-0001"),
  anEvent("0b6f1c1e-0002", {
    occurred_at: "2026-11-24T08:47:31Z",
    principal_id: "2b9e7c41-0d6a-4f3b-9c8e-1a5f3d7b9e20",
    principal_name: "ben",
    event_type: "QUERY_COMPLETED",
    event_type_raw: "QUERY_COMPLETED",
    detail: {
      sql_text: { sha256: SQL_SHA256, length: 57 },
      tables_touched: ["gold.station_ranking"],
      row_count: 12,
    },
  }),
  anEvent("0b6f1c1e-0003", {
    occurred_at: "2026-11-23T22:00:12Z",
    principal_id: "9a4c2e6b-8d0f-4a2c-b4e6-8d0f2a4c6e8b",
    principal_name: "nightly-loader",
    event_type: "TABLE_WRITE",
    event_type_raw: "TABLE_WRITE",
    securable_kind: "TABLE",
    securable_namespace: "gold",
    securable_table: "station_ranking",
    detail: { mode: "append", watermark: { hidden: true }, run_id: null },
  }),
  anEvent("0b6f1c1e-0004", {
    occurred_at: "2026-11-23T17:30:00Z",
    principal_id: null,
    principal_name: null,
    event_type: "AUTHENTICATION_FAILED",
    event_type_raw: "AUTHENTICATION_FAILED",
    securable_kind: null,
    securable_environment: null,
    detail: {},
  }),
  anEvent("0b6f1c1e-0005", {
    occurred_at: "2026-11-22T11:05:44Z",
    event_type: "UNKNOWN",
    event_type_raw: "WIDGET_FROBNICATED",
    securable_kind: "INSTANCE",
    securable_environment: null,
    detail: { action: "READ_TABLE" },
  }),
];

/** The events with the write's principal named {@link LONG_PRINCIPAL}, for a phone's width. */
export const AUDIT_EVENTS_LONG_NAME = AUDIT_EVENTS.map((event) =>
  event.principal_name === "nightly-loader" ? { ...event, principal_name: LONG_PRINCIPAL } : event,
);

/**
 * Answers the audit log with {@link AUDIT_EVENTS}, narrowed by `principal_name` and `event_type` as
 * the route does, in the order asked, on one page; and the environments `dev` and `prod`.
 * Register it after `signInAsAdmin`, whose answer for the environments it replaces.
 *
 * @param page - The page.
 * @param events - The events; {@link AUDIT_EVENTS} by default.
 */
export async function stubAudit(
  page: Page,
  events: readonly Record<string, unknown>[] = AUDIT_EVENTS,
): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/v1/audit-events",
    (route) => {
      const query = new URL(route.request().url()).searchParams;
      const who = query.get("principal_name");
      const type = query.get("event_type");
      const items = events.filter(
        (event) =>
          (who === null || event.principal_name === who) &&
          (type === null || event.event_type === type),
      );
      return route.fulfill({
        json: {
          items: query.get("order") === "asc" ? [...items].reverse() : items,
          next_cursor: null,
        },
      });
    },
  );
  await page.route(
    (url) => url.pathname === "/api/v1/environments",
    (route) =>
      route.fulfill({
        json: ["dev", "prod"].map((name) => ({
          name,
          storage_root: `file:///srv/lakana/${name}`,
          storage_secret_ref: null,
          is_protected: name === "prod",
          properties: {},
          retention_days: null,
          deleted_at: null,
          purge_after: null,
        })),
      }),
  );
}
