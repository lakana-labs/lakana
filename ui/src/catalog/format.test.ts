import { describe, expect, it } from "vitest";

import { formatBytes, formatRows, relativeTime } from "./format";

const NOW = Date.parse("2026-11-13T12:00:00Z");
const ago = (ms: number): string => new Date(NOW - ms).toISOString();

describe("catalog formats", () => {
  it("groups rows below a million and shortens them from a million", () => {
    expect(formatRows(0, "en")).toBe("0");
    expect(formatRows(48210, "en")).toBe("48,210");
    expect(formatRows(1_204_000, "en")).toBe("1.2M");
    expect(formatRows(48210, "de")).toBe("48.210");
  });

  it("writes sizes in decimal units", () => {
    expect(formatBytes(0, "en")).toBe("0 B");
    expect(formatBytes(512, "en")).toBe("512 B");
    expect(formatBytes(98_304, "en")).toBe("98.3 kB");
    expect(formatBytes(3_100_000, "en")).toBe("3.1 MB");
    expect(formatBytes(2_500_000_000, "en")).toBe("2.5 GB");
    expect(formatBytes(999_999, "en")).toBe("1 MB");
    expect(formatBytes(98_304, "de")).toBe("98,3 kB");
  });

  it("is null under a minute, then minutes, hours, days, months and years", () => {
    expect(relativeTime(ago(30_000), NOW, "en")).toBeNull();
    expect(relativeTime(ago(-5_000), NOW, "en")).toBeNull();
    expect(relativeTime(ago(5 * 60_000), NOW, "en")).toBe("5 minutes ago");
    expect(relativeTime(ago(7_200_000), NOW, "en")).toBe("2 hours ago");
    expect(relativeTime(ago(3 * 86_400_000), NOW, "en")).toBe("3 days ago");
    expect(relativeTime(ago(65 * 86_400_000), NOW, "en")).toBe("2 months ago");
    expect(relativeTime(ago(800 * 86_400_000), NOW, "en")).toBe("2 years ago");
  });

  it("is null for a time it cannot read", () => {
    expect(relativeTime("not a time", NOW, "en")).toBeNull();
  });
});
