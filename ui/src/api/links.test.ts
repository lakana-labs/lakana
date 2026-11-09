import { describe, expect, it } from "vitest";

import { send } from "./client";
import { describeLink, redeemLink } from "./links";

import { fakeServer } from "../test/server";

describe("links", () => {
  it("describes a link by its public id only", async () => {
    const server = fakeServer({
      "POST /api/v1/sign-in/links/describe": {
        status: 200,
        body: { kind: "invite", name: "kim", display_name: "Kim", state: "open" },
      },
    });

    const described = await describeLink("pub-1");

    expect(described).toEqual({ kind: "invite", name: "kim", display_name: "Kim", state: "open" });
    expect(server.sent[0]?.body).toEqual({ link_id: "pub-1" });
  });

  it("uses a link with its secret in the body and keeps the new CSRF token", async () => {
    const server = fakeServer({
      "POST /api/v1/sign-in/links/redeem": {
        status: 200,
        body: { name: "kim", needs_passkey: true, offer_passkey: false, csrf_token: "csrf-9" },
      },
      "POST /api/v1/things": { status: 200, body: {} },
    });

    const signedIn = await redeemLink("pub-1", "sec-1");
    await send("POST", "/api/v1/things", {});

    expect(signedIn.needs_passkey).toBe(true);
    expect(server.sent[0]).toMatchObject({
      path: "/api/v1/sign-in/links/redeem",
      body: { link_id: "pub-1", secret: "sec-1" },
    });
    expect(server.sent[1]?.headers["x-lakana-csrf"]).toBe("csrf-9");
  });
});
