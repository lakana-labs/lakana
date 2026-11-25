import { describe, expect, it } from "vitest";

import { localeNumbers } from "./numbers";

describe("localeNumbers", () => {
  it("writes a number for the language, with the options of its format", () => {
    expect(localeNumbers.format(12_400, undefined, "en")).toBe("12,400");
    expect(localeNumbers.format(12_400, " ", "de")).toBe("12.400");
    expect(localeNumbers.format(0.25, "number(style: percent)", "en")).toBe("25%");
    expect(localeNumbers.format(2.5, "number(minimumFractionDigits: 2; )", "de")).toBe("2,50");
  });

  it("leaves a value that is not a number as it is", () => {
    expect(localeNumbers.format("v12400", undefined, "en")).toBe("v12400");
    expect(localeNumbers.format(undefined, undefined, "en")).toBeUndefined();
  });

  it("refuses a format other than number, and custom formats", () => {
    expect(() => localeNumbers.format(1, "datetime", "en")).toThrow('No format "datetime"');
    expect(() => {
      localeNumbers.add("upper", (value: string) => value.toUpperCase());
    }).toThrow("Messages format numbers only.");
    expect(() => {
      localeNumbers.addCached("upper", () => (value: string) => value.toUpperCase());
    }).toThrow("Messages format numbers only.");
  });
});
