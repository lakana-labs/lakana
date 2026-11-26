import { describe, expect, it } from "vitest";

import { AUDIT_PAGE, listAuditEvents, NO_AUDIT_FILTER } from "./audit";
import { ApiError } from "./client";

import { fakeServer } from "../test/server";

const EVENT = {
  id: "0b6f1c1e-4a55-4f0e-9a43-6d4f4c1b2a10",
  occurred_at: "2026-11-24T09:12:00Z",
  principal_id: "7d3a3b6e-1f0c-4c55-8a3e-2e9f5b1c0a11",
  principal_name: "maria",
  event_type: "GRANT_CREATED",
  event_type_raw: "GRANT_CREATED",
  securable_kind: "ENVIRONMENT",
  securable_environment: "dev",
  securable_namespace: null,
  securable_table: null,
  detail: { role: "ANALYST", principal: "ben" },
};

describe("audit", () => {
  it("reads the first page, newest first, with no filter", async () => {
    const server = fakeServer({
      "GET /api/v1/audit-events": { status: 200, body: { items: [EVENT], next_cursor: "c2" } },
    });

    expect(await listAuditEvents(NO_AUDIT_FILTER, null, null)).toEqual({
      items: [EVENT],
      nextCursor: "c2",
    });
    expect(server.sent.map((request) => request.path)).toEqual([
      `/api/v1/audit-events?limit=${String(AUDIT_PAGE)}`,
    ]);
  });

  it("sends every filter, the order and the cursor", async () => {
    const server = fakeServer({
      "GET /api/v1/audit-events": { status: 200, body: { items: [], next_cursor: null } },
    });

    await listAuditEvents(
      {
        principal: "maria",
        eventType: "GRANT_CREATED",
        environment: "dev",
        since: "2026-11-01T00:00:00.000Z",
      },
      { sort: "occurred", order: "asc" },
      "c2",
    );
    const sent = new URL(server.sent[0]?.path ?? "", "http://lakana.test").searchParams;
    expect(Object.fromEntries(sent)).toEqual({
      limit: String(AUDIT_PAGE),
      principal_name: "maria",
      event_type: "GRANT_CREATED",
      environment: "dev",
      since: "2026-11-01T00:00:00.000Z",
      sort: "occurred",
      order: "asc",
      cursor: "c2",
    });
  });

  it("passes a refusal on as an ApiError", async () => {
    fakeServer({
      "GET /api/v1/audit-events": {
        status: 403,
        body: { type: "PermissionDenied", detail: "You need READ_AUDIT_LOG on the instance." },
      },
    });

    const refusal = await listAuditEvents(NO_AUDIT_FILTER, null, null).catch(
      (error: unknown) => error,
    );
    expect(refusal).toBeInstanceOf(ApiError);
    expect(refusal).toMatchObject({ status: 403 });
  });
});
