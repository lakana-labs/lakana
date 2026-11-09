import { describe, expect, it } from "vitest";

import { changeSignInSettings, getSignInSettings } from "./settings";

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
  it("reads and changes the session lengths", async () => {
    const server = fakeServer({
      "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
      "PATCH /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
    });

    expect(await getSignInSettings()).toEqual(SETTINGS);
    await changeSignInSettings({ idle_timeout_minutes: 60, absolute_lifetime_minutes: 120 });

    expect(server.sent[1]).toMatchObject({
      method: "PATCH",
      body: { idle_timeout_minutes: 60, absolute_lifetime_minutes: 120 },
    });
  });
});
