import { describe, expect, it, vi } from "vitest";

import { CONTENT_SECURITY_POLICY } from "../csp";
import { CSP_FILE, cspFile } from "./csp-file";

describe("cspFile", () => {
  it("writes the policy into the build, for the server to send", () => {
    const emitFile = vi.fn();

    cspFile().generateBundle.call({ emitFile });

    expect(emitFile).toHaveBeenCalledExactlyOnceWith({
      type: "asset",
      fileName: CSP_FILE,
      source: CONTENT_SECURITY_POLICY,
    });
  });

  it("names a file at the top of the build", () => {
    expect(CSP_FILE).toBe("csp.txt");
    expect(cspFile().name).toBe("lakana-csp-file");
  });
});
