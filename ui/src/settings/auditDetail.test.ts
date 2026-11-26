import { describe, expect, it } from "vitest";

import type { AuditEventOut } from "../api/audit";
import { detailFields, detailValue, eventObject } from "./auditDetail";

const SHA = "a".repeat(64);

/**
 * An event with the given changes.
 *
 * @param change - What differs from a grant in `dev`.
 * @returns The event.
 */
function event(change: Partial<AuditEventOut>): AuditEventOut {
  return {
    id: "e1",
    occurred_at: "2026-11-24T09:12:00Z",
    principal_id: "p1",
    principal_name: "maria",
    event_type: "GRANT_CREATED",
    event_type_raw: "GRANT_CREATED",
    securable_kind: "ENVIRONMENT",
    securable_environment: "dev",
    securable_namespace: null,
    securable_table: null,
    detail: {},
    ...change,
  };
}

describe("an audit event's fields", () => {
  it("shows text as it is and other values as JSON", () => {
    expect(detailValue("ANALYST")).toEqual({ kind: "text", text: "ANALYST" });
    expect(detailValue(42)).toEqual({ kind: "text", text: "42" });
    expect(detailValue(false)).toEqual({ kind: "text", text: "false" });
    expect(detailValue(["gold.orders"])).toEqual({ kind: "text", text: '["gold.orders"]' });
    expect(detailValue({ a: 1 })).toEqual({ kind: "text", text: '{"a":1}' });
  });

  it("names a missing value", () => {
    expect(detailValue(null)).toEqual({ kind: "none" });
    expect(detailValue(undefined)).toEqual({ kind: "none" });
  });

  it("names what the server withheld", () => {
    expect(detailValue({ hidden: true })).toEqual({ kind: "withheld" });
    expect(detailValue({ hidden: false })).toEqual({ kind: "text", text: '{"hidden":false}' });
    expect(detailValue({ hidden: true, x: 1 })).toEqual({
      kind: "text",
      text: '{"hidden":true,"x":1}',
    });
  });

  it("names a fingerprint, and only a true one", () => {
    expect(detailValue({ sha256: SHA, length: 57 })).toEqual({
      kind: "fingerprint",
      sha256: SHA,
      length: 57,
    });
    expect(detailValue({ sha256: "zz", length: 57 }).kind).toBe("text");
    expect(detailValue({ sha256: SHA, length: "57" }).kind).toBe("text");
    expect(detailValue({ sha256: 1, length: 57 }).kind).toBe("text");
  });

  it("keeps the server's order of the fields", () => {
    expect(
      detailFields(event({ detail: { role: "ANALYST", principal: "ben", note: null } })),
    ).toEqual([
      { name: "role", value: { kind: "text", text: "ANALYST" } },
      { name: "principal", value: { kind: "text", text: "ben" } },
      { name: "note", value: { kind: "none" } },
    ]);
  });
});

describe("the object an audit event is about", () => {
  it("is none, the instance, an environment, a namespace or a table", () => {
    expect(eventObject(event({ securable_kind: null, securable_environment: null }))).toBeNull();
    expect(eventObject(event({ securable_kind: "INSTANCE", securable_environment: null }))).toBe(
      "instance",
    );
    expect(eventObject(event({ securable_kind: "ENVIRONMENT", securable_environment: null }))).toBe(
      "instance",
    );
    expect(eventObject(event({}))).toEqual({ environment: "dev", rest: null });
    expect(
      eventObject(event({ securable_kind: "NAMESPACE", securable_namespace: "gold.sales" })),
    ).toEqual({ environment: "dev", rest: "gold.sales" });
    expect(
      eventObject(
        event({
          securable_kind: "TABLE",
          securable_namespace: "gold",
          securable_table: "station_ranking",
        }),
      ),
    ).toEqual({ environment: "dev", rest: "gold.station_ranking" });
  });
});
