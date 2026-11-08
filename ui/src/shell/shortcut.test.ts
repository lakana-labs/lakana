import { describe, expect, it } from "vitest";

import { shortcutLabel } from "./shortcut";

describe("shortcutLabel", () => {
  it.each([
    ["MacIntel", "mac"],
    ["iPhone", "mac"],
    ["iPad", "mac"],
    ["Win32", "other"],
    ["Linux x86_64", "other"],
  ] as const)("%s shows the %s shortcut", (platform, expected) => {
    expect(shortcutLabel({ platform, userAgent: "" })).toBe(expected);
  });

  it("reads the user agent when the platform is empty", () => {
    expect(
      shortcutLabel({ platform: "", userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0)" }),
    ).toBe("mac");
    expect(shortcutLabel({ platform: "", userAgent: "Mozilla/5.0 (X11; Linux x86_64)" })).toBe(
      "other",
    );
  });

  it("prefers the client hints platform", () => {
    expect(
      shortcutLabel({ platform: "Win32", userAgent: "", userAgentData: { platform: "macOS" } }),
    ).toBe("mac");
    expect(
      shortcutLabel({
        platform: "MacIntel",
        userAgent: "",
        userAgentData: { platform: "Windows" },
      }),
    ).toBe("other");
  });
});
