// Proves that every gate of the interface still fails on a violation (docs/agent/testing.md,
// "The ui/ gates"). A gate that passes everything would be green forever and guard nothing.
//
// It copies ui/ (without node_modules and reports) and the built interface into a scratch root
// that mirrors the repository ($GATE_PROBE_DIR, default ~/.cache/lakana-ui-gate-probe), links
// node_modules, and for each gate writes one violation, runs the gate, and requires a non-zero
// exit and the expected text. The repository itself is never written to. Run it after
// `npm run build`: the size probe measures the build.
import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const UI = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO = resolve(UI, "..");
const DIST = "packages/lakana-server/src/lakana_server/ui_dist";
const SCRATCH = resolve(
  process.env.GATE_PROBE_DIR ?? join(homedir(), ".cache", "lakana-ui-gate-probe"),
);
const PROBE_UI = join(SCRATCH, "ui");
const JSCPD_REPORT = join(SCRATCH, "jscpd-report-ui");
const TIMEOUT_MS = 180_000;
const NOT_COPIED = new Set(["node_modules", "coverage", "test-results", "playwright-report"]);

// An 80-token function: above jscpd's --min-tokens 70, so two copies are a clone.
const COPIED_FUNCTION = `
export function summarise(values: readonly number[]): { total: number; largest: number; mean: number } {
  let total = 0;
  let largest = Number.NEGATIVE_INFINITY;
  for (const value of values) {
    total += value;
    if (value > largest) {
      largest = value;
    }
  }
  const mean = values.length === 0 ? 0 : total / values.length;
  return { total: total * 2 - total, largest: Math.max(largest, 0), mean: Math.round(mean * 100) / 100 };
}
`;

/**
 * @typedef {object} Command
 * @property {string} program - the executable
 * @property {string[]} args - its arguments
 * @property {string} cwd - where it runs
 */

/**
 * @typedef {object} Probe
 * @property {string} gate - the gate's name, as the output prints it
 * @property {Record<string, string>} files - files to write, relative to the scratch ui/
 * @property {Record<string, (text: string) => string>} [edits] - files to change, the same way
 * @property {Command[]} [before] - commands to run first, whatever their exit
 * @property {Command} run - the gate; it must fail
 * @property {string[]} expect - texts its output must contain; a text listed twice must appear twice
 */

/**
 * Run an npm script in the scratch copy.
 *
 * @param {string} script - the npm script
 * @returns {Command} the command
 */
function npmRun(script) {
  return { program: "npm", args: ["run", "-s", script], cwd: PROBE_UI };
}

/** @type {Probe[]} */
const PROBES = [
  {
    gate: "ESLint strict-type-checked",
    files: {
      "src/probe/unsafe.ts":
        '/**\n * Probe.\n *\n * @returns A value.\n */\nexport function probe(): unknown {\n  return (JSON.parse("{}") as any).value;\n}\n',
    },
    run: npmRun("lint"),
    expect: ["no-unsafe-member-access"],
  },
  {
    gate: "ESLint react-hooks",
    files: {
      "src/probe/Hooks.tsx":
        'import { useState } from "react";\n\n/**\n * Probe.\n *\n * @param props - The props.\n * @param props.on - Whether.\n * @returns A number.\n */\nexport function Probe({ on }: { readonly on: boolean }): number {\n  if (on) {\n    const [n] = useState(0);\n    return n;\n  }\n  return 0;\n}\n',
    },
    run: npmRun("lint"),
    expect: ["react-hooks/rules-of-hooks"],
  },
  {
    gate: "ESLint jsx-a11y",
    files: {
      "src/probe/Image.tsx":
        'import type { JSX } from "react";\n\n/**\n * Probe.\n *\n * @returns An image.\n */\nexport function Probe(): JSX.Element {\n  return <img src="x.png" />;\n}\n',
    },
    run: npmRun("lint"),
    expect: ["jsx-a11y/alt-text"],
  },
  {
    gate: "ESLint jsdoc (TSDoc)",
    files: {
      "src/probe/undocumented.ts":
        "export function probe(): number {\n  return 1;\n}\n\n/** The props of a probe. */\nexport interface ProbeProps {\n  readonly value: number;\n}\n",
    },
    run: npmRun("lint"),
    expect: ["jsdoc/require-jsdoc", "jsdoc/require-jsdoc"],
  },
  {
    gate: "ESLint fetch rule",
    files: { "src/shell/probe.ts": 'void fetch("/api/v1/whoami");\n' },
    run: npmRun("lint"),
    expect: ["no-restricted-globals"],
  },
  {
    gate: "Prettier",
    files: { "src/probe/format.ts": "export const probe   =  1\n" },
    run: npmRun("format:check"),
    expect: ["src/probe/format.ts"],
  },
  {
    gate: "tsc",
    files: {
      "src/probe/types.ts":
        'export const n: number = "a";\nconst xs: number[] = [];\nexport const first: number = xs[0];\ninterface Options {\n  a?: string;\n}\nexport const options: Options = { a: undefined };\n',
    },
    run: npmRun("typecheck"),
    expect: ["TS2322", "TS2322", "TS2375"],
  },
  {
    gate: "dependency-cruiser",
    files: {
      "src/api/probe.ts": 'import { App } from "../App";\n\nexport const probe = App;\n',
      "src/probe/a.ts": 'import { b } from "./b";\n\nexport const a = (): number => b() + 1;\n',
      "src/probe/b.ts": 'import { a } from "./a";\n\nexport const b = (): number => a() - 1;\n',
    },
    run: npmRun("depcruise"),
    expect: ["api-is-a-leaf", "no-circular"],
  },
  {
    gate: "knip",
    files: { "src/probe/unused.ts": "export const probe = 1;\n" },
    run: npmRun("knip"),
    expect: ["Unused files"],
  },
  {
    gate: "Vitest + coverage ratchet",
    files: {
      "src/probe/branches.ts":
        'export function sign(value: number): string {\n  if (value < 0) {\n    return "negative";\n  }\n  return "positive";\n}\n',
    },
    before: [npmRun("test:coverage")],
    run: {
      program: "python3",
      args: [
        "-m",
        "scripts.ui_coverage_ratchet",
        "--summary",
        join(PROBE_UI, "coverage", "coverage-summary.json"),
        "--baseline",
        join(REPO, "ui", ".coverage-baseline"),
        "--source",
        join(PROBE_UI, "src"),
      ],
      cwd: REPO,
    },
    expect: ["below"],
  },
  {
    gate: "size-limit",
    files: {},
    edits: { ".size-limit.json": (text) => text.replace(/"limit": "[^"]*"/g, '"limit": "1 B"') },
    run: npmRun("size"),
    expect: ["exceeded"],
  },
  {
    gate: "jscpd + duplication ratchet",
    files: { "src/probe/copy1.ts": COPIED_FUNCTION, "src/probe/copy2.ts": COPIED_FUNCTION },
    before: [
      {
        program: join(REPO, "tools", "jscpd", "node_modules", ".bin", "jscpd"),
        args: [
          join(PROBE_UI, "src"),
          "--format",
          "typescript,tsx",
          "--ignore",
          "**/*.test.ts,**/*.test.tsx",
          "--min-tokens",
          "70",
          "--reporters",
          "json",
          "--output",
          JSCPD_REPORT,
          "--silent",
        ],
        cwd: SCRATCH,
      },
    ],
    run: {
      program: "python3",
      args: [
        join(REPO, "scripts", "duplication_ratchet.py"),
        "--report",
        join(JSCPD_REPORT, "jscpd-report.json"),
        "--baseline",
        join(REPO, "scripts", "duplication-baseline-ui.json"),
      ],
      cwd: REPO,
    },
    expect: ["Duplication rose"],
  },
];

/**
 * Run one command, its output captured.
 *
 * @param {Command} command - what to run
 * @returns {{status: number | null, output: string}} the exit status (null on a timeout) and the output
 */
function execute(command) {
  const result = spawnSync(command.program, command.args, {
    cwd: command.cwd,
    encoding: "utf8",
    timeout: TIMEOUT_MS,
    maxBuffer: 64 * 1024 * 1024,
  });
  const error = result.error === undefined ? "" : String(result.error);
  return { status: result.status, output: `${result.stdout}${result.stderr}${error}` };
}

/**
 * Count the occurrences of a text in another.
 *
 * @param {string} haystack - the text searched
 * @param {string} needle - the text counted
 * @returns {number} how often it occurs
 */
function occurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

/**
 * Copy ui/ and the built interface into the scratch root, node_modules linked.
 */
function prepareScratch() {
  if (!existsSync(join(REPO, DIST, "index.html"))) {
    console.error(`No build in ${DIST}: run \`npm --prefix ui run build\` first.`);
    process.exit(1);
  }
  const inside = relative(REPO, SCRATCH);
  if (inside === "" || !inside.startsWith("..")) {
    console.error(
      `GATE_PROBE_DIR (${SCRATCH}) is inside the repository; choose a scratch directory outside it.`,
    );
    process.exit(1);
  }
  for (const made of [PROBE_UI, join(SCRATCH, "packages"), JSCPD_REPORT]) {
    rmSync(made, { recursive: true, force: true });
  }
  mkdirSync(SCRATCH, { recursive: true });
  cpSync(UI, PROBE_UI, {
    recursive: true,
    filter: (source) => !NOT_COPIED.has(relative(UI, source).split("/")[0] ?? ""),
  });
  cpSync(join(REPO, DIST), join(SCRATCH, DIST), { recursive: true });
  symlinkSync(join(UI, "node_modules"), join(PROBE_UI, "node_modules"), "dir");
}

/**
 * Run one probe in the scratch copy and undo its changes.
 *
 * @param {Probe} probe - the violation and the gate
 * @returns {string | null} why the gate let the violation through, or null when it caught it
 */
function prove(probe) {
  /** @type {Record<string, string>} */
  const originals = {};
  for (const [path, edit] of Object.entries(probe.edits ?? {})) {
    const file = join(PROBE_UI, path);
    originals[path] = readFileSync(file, "utf8");
    writeFileSync(file, edit(originals[path]));
  }
  for (const [path, content] of Object.entries(probe.files)) {
    mkdirSync(dirname(join(PROBE_UI, path)), { recursive: true });
    writeFileSync(join(PROBE_UI, path), content);
  }
  try {
    for (const command of probe.before ?? []) {
      execute(command);
    }
    const { status, output } = execute(probe.run);
    if (status === 0) {
      return "it exited 0";
    }
    if (status === null) {
      return `it did not finish within ${String(TIMEOUT_MS / 1000)} s: ${output.slice(-300)}`;
    }
    for (const text of new Set(probe.expect)) {
      const wanted = occurrences(probe.expect.join("\n"), text);
      if (occurrences(output, text) < wanted) {
        return `it failed, but its output lacks "${text}" (${String(wanted)}x): ${output.slice(-600)}`;
      }
    }
    return null;
  } finally {
    for (const path of Object.keys(probe.files)) {
      rmSync(join(PROBE_UI, path), { force: true });
    }
    for (const [path, text] of Object.entries(originals)) {
      writeFileSync(join(PROBE_UI, path), text);
    }
  }
}

prepareScratch();
const leaks = [];
for (const probe of PROBES) {
  const leak = prove(probe);
  if (leak === null) {
    console.log(
      `ok   ${probe.gate}: failed on its violation, as it should (${[...new Set(probe.expect)].join(", ")})`,
    );
  } else {
    console.log(`LEAK ${probe.gate}: let its violation through, ${leak}`);
    leaks.push(probe.gate);
  }
}
if (leaks.length > 0) {
  console.error(
    `\nThese gates no longer fail on a violation: ${leaks.join(", ")}. Fix the gate, not the probe.`,
  );
  process.exit(1);
}
console.log(`\nAll ${String(PROBES.length)} gates fail on a violation.`);
