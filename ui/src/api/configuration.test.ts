import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import { getConfiguration } from "./configuration";

import { fakeServer } from "../test/server";

const SETTINGS = [
  { name: "LAKANA_PORT", value: 8000, secret: false, has_value: true, source: "set" },
  { name: "LAKANA_SECRETS__MASTER_KEY", value: null, secret: true, has_value: true, source: "set" },
];

describe("configuration", () => {
  it("reads the configuration", async () => {
    const server = fakeServer({
      "GET /api/v1/settings/configuration": { status: 200, body: SETTINGS },
    });

    expect(await getConfiguration()).toEqual(SETTINGS);
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/settings/configuration",
    ]);
  });

  it("passes a 403 on as an ApiError", async () => {
    fakeServer({
      "GET /api/v1/settings/configuration": {
        status: 403,
        body: { type: "PermissionDenied", detail: "Only an administrator may read it." },
      },
    });

    const refusal = await getConfiguration().catch((error: unknown) => error);
    expect(refusal).toBeInstanceOf(ApiError);
    expect(refusal).toMatchObject({ status: 403, detail: "Only an administrator may read it." });
  });
});
