import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { AUDIT_EVENT_TYPES } from "./auditEventTypes";

/** The part of the committed REST reference this test reads. */
interface Reference {
  readonly components: {
    readonly schemas: Record<string, { readonly enum?: readonly string[] }>;
  };
}

const reference = JSON.parse(
  readFileSync(resolve(import.meta.dirname, "../../../docs/site/reference/openapi.json"), "utf8"),
) as Reference;

describe("the audit log's event types", () => {
  it("are the server's filter values, in its order", () => {
    expect(AUDIT_EVENT_TYPES).toEqual(reference.components.schemas.AuditEventFilter?.enum);
  });

  it("leave out UNKNOWN, which is no filter", () => {
    expect(AUDIT_EVENT_TYPES).not.toContain("UNKNOWN");
  });
});
