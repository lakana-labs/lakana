import { describe, expect, it } from "vitest";

import { mySettingsFacts } from "./settingsFacts";

import { fakeServer } from "../test/server";

describe("mySettingsFacts", () => {
  it("reads both facts in one whoami read", async () => {
    const server = fakeServer({
      "GET /api/v1/whoami": {
        status: 200,
        body: { secret_environments: ["dev"], manage_policy: true },
      },
    });

    await expect(mySettingsFacts()).resolves.toEqual({
      secretEnvironments: ["dev"],
      managePolicy: true,
    });
    expect(server.sent).toHaveLength(1);
  });

  it("reads none and no from a server that does not say", async () => {
    fakeServer({ "GET /api/v1/whoami": { status: 200, body: { name: "kim" } } });

    await expect(mySettingsFacts()).resolves.toEqual({
      secretEnvironments: [],
      managePolicy: false,
    });
  });
});
