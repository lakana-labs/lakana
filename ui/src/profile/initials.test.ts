import { describe, expect, it } from "vitest";

import { initials } from "./initials";

describe("initials", () => {
  it("takes the first letters of the display name's first and last word", () => {
    expect(initials("klee", "Kim Lee")).toBe("KL");
    expect(initials("klee", "kim  van der lee")).toBe("KL");
  });

  it("splits the principal name at dots, underscores, dashes and spaces without one", () => {
    expect(initials("kim.lee", null)).toBe("KL");
    expect(initials("kim_lee", null)).toBe("KL");
    expect(initials("kim-lee", "  ")).toBe("KL");
    expect(initials("admin", null)).toBe("A");
  });

  it("keeps a letter outside ASCII whole", () => {
    expect(initials("x", "Øystein Ådne")).toBe("ØÅ");
    expect(initials("x", "𝓐lex")).toBe("𝓐");
  });

  it("answers a question mark for a name with nothing to take", () => {
    expect(initials("...", null)).toBe("?");
  });
});
