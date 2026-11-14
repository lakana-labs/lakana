import { describe, expect, it } from "vitest";

import { shellQuote } from "./shellQuote";

describe("shellQuote", () => {
  it("quotes the way a POSIX shell reads it", () => {
    expect(shellQuote("analytics")).toBe("'analytics'");
    expect(shellQuote("it's $(rm -rf ~)")).toBe("'it'\\''s $(rm -rf ~)'");
  });
});
