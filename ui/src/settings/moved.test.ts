import { describe, expect, it } from "vitest";

import { movedAccessPath } from "./moved";

describe("movedAccessPath", () => {
  it.each([
    ["/access", "/settings/access/people"],
    ["/access/", "/settings/access/people"],
    ["/access/people", "/settings/access/people"],
    ["/access/people/", "/settings/access/people"],
    ["/access/people/kim", "/settings/access/people/kim"],
    ["/access/people/kim%20lee", "/settings/access/people/kim%20lee"],
    ["/access/invites", "/settings/sign-in/invites"],
    ["/access/invites/", "/settings/sign-in/invites"],
    ["/access/recovery-links", "/settings/sign-in/recovery-links"],
    ["/access/unknown", "/settings/access/unknown"],
    ["/Access/Invites", "/settings/sign-in/invites"],
    ["/ACCESS/People/Kim%20Lee", "/settings/access/people/Kim%20Lee"],
  ])("moves every old Access address (%s)", (old, moved) => {
    expect(movedAccessPath(old)).toBe(moved);
  });
});
