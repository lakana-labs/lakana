import { describe, expect, it } from "vitest";

import { getSignInSettings } from "./settings";

import { fakeServer } from "../test/server";

const SETTINGS = {
  idle_timeout_minutes: 720,
  absolute_lifetime_minutes: 10080,
  bounds: {
    idle_timeout_minutes_min: 15,
    idle_timeout_minutes_max: 1440,
    absolute_lifetime_minutes_min: 60,
    absolute_lifetime_minutes_max: 43200,
  },
};

describe("settings", () => {
  it("reads the session lengths", async () => {
    const server = fakeServer({ "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } });

    expect(await getSignInSettings()).toEqual(SETTINGS);
    expect(server.sent[0]).toMatchObject({ method: "GET", path: "/api/v1/settings/sign-in" });
  });
});
