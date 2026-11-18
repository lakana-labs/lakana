import { describe, expect, it } from "vitest";

import { setStepUp } from "./client";
import {
  addMember,
  createGroup,
  deleteGroup,
  listGroups,
  listMembers,
  removeMember,
} from "./groups";

import { fakeServer } from "../test/server";

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

describe("groups", () => {
  it("lists by words and member", async () => {
    const server = fakeServer({
      "GET /api/v1/groups": {
        status: 200,
        body: [{ name: "analysts" }],
        headers: { "X-Next-Cursor": "c2" },
      },
    });

    const first = await listGroups({ words: " ana ", member: null }, null);
    await listGroups({ words: "", member: "maria" }, "c2");

    expect(first).toEqual({ items: [{ name: "analysts" }], nextCursor: "c2" });
    expect(server.sent.map((request) => request.path)).toEqual([
      "/api/v1/groups?q=ana&limit=50",
      "/api/v1/groups?member=maria&limit=50&cursor=c2",
    ]);
  });

  it("deletes and changes members with the passkey asked first", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "DELETE /api/v1/groups/an al": { status: 204 },
      "PUT /api/v1/groups/an al/members/ci bot": { status: 204 },
      "DELETE /api/v1/groups/an al/members/ci bot": { status: 204 },
    });
    setStepUp(() => Promise.resolve());

    await addMember("an al", "ci bot");
    await removeMember("an al", "ci bot");
    await deleteGroup("an al");

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "PUT /api/v1/groups/an%20al/members/ci%20bot",
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/groups/an%20al/members/ci%20bot",
      "GET /api/v1/sign-in/session",
      "DELETE /api/v1/groups/an%20al",
    ]);
  });

  it("creates without asking for a touch", async () => {
    const server = fakeServer({
      "POST /api/v1/groups": { status: 201, body: { name: "finance" } },
    });
    setStepUp(() => Promise.resolve());

    await expect(createGroup("finance")).resolves.toEqual({ name: "finance" });
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "POST /api/v1/groups",
    ]);
    expect(server.sent[0]?.body).toEqual({ name: "finance" });
  });

  it("lists a group's members", async () => {
    const members = [{ name: "maria", type: "USER", is_active: true, picture: null }];
    const server = fakeServer({
      "GET /api/v1/groups/analysts/members": { status: 200, body: members },
    });

    await expect(listMembers("analysts")).resolves.toEqual(members);
    expect(server.sent[0]?.path).toBe("/api/v1/groups/analysts/members");
  });
});
