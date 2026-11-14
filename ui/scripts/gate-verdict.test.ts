// The verdicts of prove-gates.mjs (docs/agent/testing.md, "The ui/ gates"): a gate proof that
// judges wrong guards nothing, so the judging is tested apart from the gates.
import { describe, expect, it } from "vitest";

import { sectionsByFile, verdict } from "./gate-verdict.mjs";

const STYLISH = `
/probe/ui/src/probe/unsafe.ts
  7:10  error  Unsafe member access .value on an \`any\` value  @typescript-eslint/no-unsafe-member-access

/probe/ui/src/shell/node.ts
  1:21  error  Unexpected use of 'process'  no-restricted-globals
  2:22  error  Unexpected use of 'Buffer'  no-restricted-globals

✖ 3 problems (3 errors, 0 warnings)
`;

describe("sectionsByFile", () => {
  it("gives each file only the lines about it", () => {
    const sections = sectionsByFile(STYLISH);
    expect([...sections.keys()]).toEqual([
      "/probe/ui/src/probe/unsafe.ts",
      "/probe/ui/src/shell/node.ts",
    ]);
    expect(sections.get("/probe/ui/src/probe/unsafe.ts")).toContain("no-unsafe-member-access");
    expect(sections.get("/probe/ui/src/probe/unsafe.ts")).not.toContain("no-restricted-globals");
    expect(
      sections.get("/probe/ui/src/shell/node.ts")?.split("no-restricted-globals"),
    ).toHaveLength(3);
  });

  it("finds nothing in an output that names no file", () => {
    expect(sectionsByFile("Oops! Something went wrong\n").size).toBe(0);
  });
});

describe("verdict", () => {
  const probe = { expect: ["no-restricted-globals", "no-restricted-globals"] };
  const none: never[] = [];

  it("accepts a failure that prints every expected text as often as listed", () => {
    const outcome = { status: 1, output: STYLISH, shared: false, failedBefore: none };
    expect(verdict(probe, outcome, 180)).toBeNull();
  });

  it("refuses a pass, a timeout and a failure for another reason", () => {
    expect(verdict(probe, { status: 0, output: "", shared: false, failedBefore: none }, 180)).toBe(
      "it exited 0",
    );
    expect(
      verdict(probe, { status: null, output: "", shared: false, failedBefore: none }, 180),
    ).toMatch(/^it did not finish within 180 s/);
    const once = {
      status: 1,
      output: "x no-restricted-globals",
      shared: false,
      failedBefore: none,
    };
    expect(verdict(probe, once, 180)).toMatch(/lacks "no-restricted-globals" \(2x\)/);
  });

  it("shows the end of a step before the gate that failed", () => {
    const outcome = {
      status: 1,
      output: "No such file: coverage-summary.json",
      shared: false,
      failedBefore: [
        { label: "npm run -s test:coverage", status: 1, output: "x".repeat(900) + "OOM" },
      ],
    };
    const leak = verdict({ expect: ["below"] }, outcome, 180);
    expect(leak).toContain("the step before it, `npm run -s test:coverage`, exited 1: ");
    expect(leak).toMatch(/xOOM$/);
    expect(leak).not.toContain("x".repeat(601));
  });

  it("accepts an allowed file only when the gate passes or, in a shared run, names it not", () => {
    const allowed = { expect: [], accept: true };
    expect(verdict(allowed, { status: 0, output: "", shared: false, failedBefore: none }, 1)).toBe(
      null,
    );
    expect(
      verdict(allowed, { status: 1, output: "", shared: false, failedBefore: none }, 1),
    ).toMatch(/refused an allowed file/);
    expect(
      verdict(allowed, { status: 1, output: "", shared: true, failedBefore: none }, 1),
    ).toBeNull();
    expect(
      verdict(allowed, { status: 1, output: "  1:6 error x", shared: true, failedBefore: none }, 1),
    ).toMatch(/refused an allowed file/);
    expect(
      verdict(allowed, { status: null, output: "", shared: true, failedBefore: none }, 1),
    ).toMatch(/refused an allowed file/);
  });
});
