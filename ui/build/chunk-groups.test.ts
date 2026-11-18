import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { SHELL_CHUNK } from "./chunk-groups";

const ROOT = resolve(import.meta.dirname, "..");

describe("the shell chunk group", () => {
  it.each([
    "src/shell/Page.tsx",
    "src/shell/NotFound.tsx",
    "node_modules/react-aria-components/dist/private/Button.mjs",
  ])("matches a file that exists (%s)", (file) => {
    expect(existsSync(resolve(ROOT, file))).toBe(true);
    expect(SHELL_CHUNK.test(resolve(ROOT, file))).toBe(true);
  });

  it("matches Windows-style paths and nothing else of the shell", () => {
    expect(SHELL_CHUNK.test("C:\\app\\src\\shell\\Page.tsx")).toBe(true);
    expect(SHELL_CHUNK.test(resolve(ROOT, "src/shell/AppShell.tsx"))).toBe(false);
  });
});
