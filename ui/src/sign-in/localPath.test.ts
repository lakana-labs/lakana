import { describe, expect, it } from "vitest";

import { isLocalPath } from "./localPath";

describe("isLocalPath", () => {
  it.each([
    "/",
    "/jobs",
    "/catalog/prod/gold.sales?q=store",
    "/sign-in/cli?port=49152&state=s-1",
    "/sign-in/device",
    "/a%20b",
  ])("keeps %j", (path) => {
    expect(isLocalPath(path)).toBe(true);
  });

  it.each([
    ["another host", "//evil.example/x"],
    ["a backslash", "/\\evil.example/x"],
    ["a tab the browser drops", "/\t/evil.example"],
    ["a line feed", "/\n/evil.example"],
    ["a carriage return", "/\r/evil.example"],
    ["a right-to-left override", "/jo‮bs"],
    ["a double slash inside", "/jobs//evil"],
    ["an encoded double slash", "/%2F%2Fevil.example"],
    ["a twice-encoded backslash", "/%255Cevil.example"],
    ["an encoded tab", "/%09/evil.example"],
    ["a sign-in page", "/sign-in"],
    ["the confirmation page", "/sign-in/oidc/confirmed"],
    ["another spelling of a sign-in page", "/SIGN-IN/cli"],
    ["a confirmation with a fragment", "/sign-in/cli#x"],
    ["an absolute address", "https://evil.example/x"],
    ["no path", ""],
    ["no string", 7],
    ["a path too long", `/${"a".repeat(2048)}`],
  ])("refuses %s", (_, path) => {
    expect(isLocalPath(path)).toBe(false);
  });

  it("keeps a malformed escape as it is", () => {
    expect(isLocalPath("/100%")).toBe(true);
  });
});
