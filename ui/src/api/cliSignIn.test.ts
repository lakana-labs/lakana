import { describe, expect, it } from "vitest";

import { isLoopbackPort, loopbackUrl } from "./cliSignIn";

describe("loopbackUrl", () => {
  it("builds only an address on 127.0.0.1", () => {
    expect(loopbackUrl(49152, { code: "c0de", state: "s&1" })).toBe(
      "http://127.0.0.1:49152/?code=c0de&state=s%261",
    );
  });

  it.each([0, 80, 1023, 65536, 8080.5, Number.NaN])("throws for port %s", (port) => {
    expect(() => loopbackUrl(port, {})).toThrow(RangeError);
    expect(isLoopbackPort(port)).toBe(false);
  });

  it("accepts both ends of the range", () => {
    expect(isLoopbackPort(1024)).toBe(true);
    expect(isLoopbackPort(65535)).toBe(true);
  });
});
