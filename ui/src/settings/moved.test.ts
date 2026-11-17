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
    ["/access/%69nvites", "/settings/sign-in/invites"],
    ["/%61ccess/%49nvites/", "/settings/sign-in/invites"],
    ["/access/%72ecovery-links", "/settings/sign-in/recovery-links"],
    ["/access/%70eople", "/settings/access/people"],
    ["/access/%50eople/Kim%20Lee", "/settings/access/people/Kim%20Lee"],
    ["/%41ccess", "/settings/access/people"],
    ["/access/%75nknown", "/settings/access/%75nknown"],
    ["/access/%E0%A4%A/invites", "/settings/access/%E0%A4%A/invites"],
  ])("moves every old Access address (%s)", (old, moved) => {
    expect(movedAccessPath(old)).toBe(moved);
  });
});
