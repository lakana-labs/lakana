/**
 * Fixtures of Settings › Audit log: events as `GET /api/v1/audit-events` answers them.
 *
 * @packageDocumentation
 */
import type { AuditEventOut } from "../api/audit";

/** A query text's SHA-256, as the server shows another person's to a reader without the right. */
export const SQL_SHA256 = "3f7a9c2e5b1d4a6f8e0c2b4d6f8a0c2e4b6d8f0a2c4e6b8d0f2a4c6e8b0d2f4a";

/**
 * An event.
 *
 * @param id - Its id.
 * @param more - What differs from maria's grant in `dev`.
 * @returns The event.
 */
export function anEvent(id: string, more: Partial<AuditEventOut> = {}): AuditEventOut {
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
    detail: { grant_id: "5c1e", principal: "ben", role: "ANALYST" },
    ...more,
  };
}

/** Five events, newest first: a grant, a query of ben's, a write, a failed sign-in, and one of a newer release. */
export const AUDIT_EVENTS: readonly AuditEventOut[] = [
  anEvent("e1"),
  anEvent("e2", {
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
  anEvent("e3", {
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
  anEvent("e4", {
    occurred_at: "2026-11-23T17:30:00Z",
    principal_id: null,
    principal_name: null,
    event_type: "AUTHENTICATION_FAILED",
    event_type_raw: "AUTHENTICATION_FAILED",
    securable_kind: null,
    securable_environment: null,
    detail: {},
  }),
  anEvent("e5", {
    occurred_at: "2026-11-22T11:05:44Z",
    event_type: "UNKNOWN",
    event_type_raw: "WIDGET_FROBNICATED",
    securable_kind: "INSTANCE",
    securable_environment: null,
    detail: { action: "READ_TABLE" },
  }),
];
