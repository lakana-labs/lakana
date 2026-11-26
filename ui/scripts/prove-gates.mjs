// Proves that every gate of the interface still fails on a violation (docs/agent/testing.md,
// "The ui/ gates"). A gate that passes everything would be green forever and guard nothing.
//
// It copies ui/ (without node_modules and reports) and the built interface into a scratch root
// that mirrors the repository ($GATE_PROBE_DIR, default ~/.cache/lakana-ui-gate-probe), links
// node_modules and the server sources Vitest reads (SERVER_SOURCES), and for each gate writes one violation, runs the gate, and requires a non-zero
// exit and the expected text. The ESLint probes share one run (ESLint takes 15-25 s a run): every
// violation is written at once and each probe's rules must be reported for its own files, under
// `--max-warnings 0`, so each one alone fails the run. The repository itself is never written to.
// Run it after `npm run build`: the size probe measures the build.
import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { sectionsByFile, verdict } from "./gate-verdict.mjs";

const UI = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO = resolve(UI, "..");
const DIST = "packages/lakana-server/src/lakana_server/ui_dist";
// Server sources Vitest reads to check that the interface has words for every value the server
// can send (src/catalog/table/health.test.ts): linked into the mirror, read only.
const SERVER_SOURCES = [
  "packages/lakana-core/src/lakana_core/catalog/health_signals.py",
  "packages/lakana-core/src/lakana_core/catalog/health_record.py",
];
const SCRATCH = resolve(
  process.env.GATE_PROBE_DIR ?? join(homedir(), ".cache", "lakana-ui-gate-probe"),
);
const PROBE_UI = join(SCRATCH, "ui");
const JSCPD_REPORT = join(SCRATCH, "jscpd-report-ui");
// Marks a directory this script made, so it never empties one it did not.
const MARKER = join(SCRATCH, ".lakana-ui-gate-probe");
// How long one gate's run may take. The slowest are ESLint (23-90 s in CI) and tsc (12-17 s); the
// coverage probes run one small file. Three minutes is a wide margin over each, inside the job's
// 10-minute budget, and a run cut by it is judged a leak.
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
 * @property {Command} run - the gate; it must fail, or pass when `accept` is set
 * @property {boolean} [accept] - the gate must accept this file (an allowed exception)
 * @property {boolean} [eslint] - an ESLint probe: it runs in the one shared ESLint run
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

// The coverage probes measure a small file and its test, not the suite: the file has enough
// covered lines that one untested branch lands the total between the ratchet's floor (90) and its
// baseline (100), so only the baseline comparison can fail it, and the other probe (a fully
// tested file stays at the baseline) shows the drop comes from that branch alone. A third probe
// runs the project's own coverage settings, with no override, to show that a file no test loads
// is still counted. The whole suite
// ran 180-270 s on CI's runners and, under load, past the 360 s these runs get (#822); the
// `ui (gates)` job still runs it with coverage in `npm run check`.
const BRANCHES = {
  "src/probe/branches.ts": `export function sign(value: number): string {
${Array.from({ length: 20 }, (_, i) => `  const step${String(i)} = value + ${String(i)};\n  void step${String(i)};`).join("\n")}
  if (value < 0) {
    return "negative";
  }
  return "positive";
}
`,
};
const TESTED_ONE_BRANCH =
  'import { expect, it } from "vitest";\n\nimport { sign } from "./branches";\n\nit("tells a positive number", () => {\n  expect(sign(1)).toBe("positive");\n});\n';
const TESTED_BOTH_BRANCHES = `${TESTED_ONE_BRANCH}\nit("tells a negative number", () => {\n  expect(sign(-1)).toBe("negative");\n});\n`;

/**
 * Run Vitest with coverage over the probe's file and its test only.
 *
 * @returns {Command} the command
 */
function coverageRun() {
  return {
    program: "npm",
    args: ["run", "-s", "test:coverage", "--", "--coverage.include=src/probe/**", "src/probe/"],
    cwd: PROBE_UI,
  };
}

/**
 * Run Vitest with the project's own coverage settings over the probe folder only.
 *
 * @returns {Command} the command
 */
function projectCoverageRun() {
  return {
    program: "npm",
    args: ["run", "-s", "test:coverage", "--", "src/probe/"],
    cwd: PROBE_UI,
  };
}

/**
 * Check that the coverage summary lists the probe's unloaded file with no covered line.
 *
 * @returns {Command} the command
 */
function unloadedFileListed() {
  const script = `
    const summary = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
    const key = Object.keys(summary).find((path) => path.endsWith("/src/probe/unloaded.ts"));
    const lines = key === undefined ? undefined : summary[key].lines;
    if (lines === undefined || lines.total === 0 || lines.covered !== 0) {
      console.error("The coverage summary does not list src/probe/unloaded.ts as uncovered.");
      process.exit(1);
    }
  `;
  return {
    program: process.execPath,
    args: ["-e", script, join(PROBE_UI, "coverage", "coverage-summary.json")],
    cwd: PROBE_UI,
  };
}

/**
 * Run the coverage ratchet on the report of the scratch copy.
 *
 * @returns {Command} the command
 */
function coverageRatchet() {
  return {
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
  };
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
    eslint: true,
    expect: ["no-unsafe-member-access"],
  },
  {
    gate: "ESLint react-hooks",
    files: {
      "src/probe/Hooks.tsx":
        'import { useState } from "react";\n\n/**\n * Probe.\n *\n * @param props - The props.\n * @param props.on - Whether.\n * @returns A number.\n */\nexport function Probe({ on }: { readonly on: boolean }): number {\n  if (on) {\n    const [n] = useState(0);\n    return n;\n  }\n  return 0;\n}\n',
    },
    run: npmRun("lint"),
    eslint: true,
    expect: ["react-hooks/rules-of-hooks"],
  },
  {
    gate: "ESLint jsx-a11y",
    files: {
      "src/probe/Image.tsx":
        'import type { JSX } from "react";\n\n/**\n * Probe.\n *\n * @returns An image.\n */\nexport function Probe(): JSX.Element {\n  return <img src="x.png" />;\n}\n',
    },
    run: npmRun("lint"),
    eslint: true,
    expect: ["jsx-a11y/alt-text"],
  },
  {
    gate: "ESLint jsdoc (TSDoc)",
    files: {
      "src/probe/undocumented.ts":
        "export function probe(): number {\n  return 1;\n}\n\n/** The props of a probe. */\nexport interface ProbeProps {\n  readonly value: number;\n}\n",
    },
    run: npmRun("lint"),
    eslint: true,
    expect: ["jsdoc/require-jsdoc", "jsdoc/require-jsdoc"],
  },
  {
    gate: "ESLint fetch rule",
    files: { "src/shell/probe.ts": 'void fetch("/api/v1/whoami");\n' },
    run: npmRun("lint"),
    eslint: true,
    expect: ["no-restricted-globals"],
  },
  {
    gate: "ESLint fetch rule (properties)",
    files: {
      "src/shell/beacon.ts":
        'navigator.sendBeacon("/v1/audit");\nvoid window.fetch("/api/v1/whoami");\n',
    },
    run: npmRun("lint"),
    eslint: true,
    expect: ["no-restricted-properties", "no-restricted-properties"],
  },
  {
    gate: "ESLint Node globals in browser code",
    files: {
      "src/shell/node.ts":
        'export const mode = process.env["NODE_ENV"];\nexport const bytes = Buffer.from("a");\n',
    },
    run: npmRun("lint"),
    eslint: true,
    expect: ["no-restricted-globals", "no-restricted-globals"],
  },
  {
    gate: "ESLint HTTP client packages",
    files: { "src/shell/client.ts": 'import axios from "axios";\n\nexport const probe = axios;\n' },
    run: npmRun("lint"),
    eslint: true,
    expect: ["no-restricted-imports"],
  },
  {
    gate: "ESLint literal text",
    files: {
      "src/probe/Words.tsx":
        'import type { JSX } from "react";\n\n/**\n * Probe.\n *\n * @returns A button.\n */\nexport function Probe(): JSX.Element {\n  return <button type="button" aria-label="Close the dialog">Close</button>;\n}\n',
      "src/probe/Empty.tsx":
        'import type { JSX } from "react";\n\n/**\n * Probe.\n *\n * @param props - The words.\n * @param props.heading - The heading.\n * @returns A heading.\n */\nfunction Empty({ heading }: { readonly heading: string }): JSX.Element {\n  return <h2>{heading}</h2>;\n}\n\n/**\n * Probe.\n *\n * @returns An empty state.\n */\nexport function Probe(): JSX.Element {\n  return <Empty heading="No tables yet" />;\n}\n',
    },
    run: npmRun("lint"),
    eslint: true,
    expect: ["lakana/no-literal-text", "lakana/no-literal-text", "lakana/no-literal-text"],
  },
  {
    gate: "ESLint literal text (tests exempt)",
    files: {
      "src/probe/Words.test.tsx":
        'import { render } from "@testing-library/react";\nimport { it } from "vitest";\n\nit("renders", () => {\n  render(<button type="button" aria-label="Close the dialog">Close</button>);\n});\n',
    },
    run: npmRun("lint"),
    eslint: true,
    accept: true,
    expect: [],
  },
  {
    gate: "ESLint fetch rule (src/api/ exempt)",
    files: { "src/api/whoami.ts": 'void fetch("/api/v1/whoami");\n' },
    run: npmRun("lint"),
    eslint: true,
    accept: true,
    expect: [],
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
    files: { ...BRANCHES, "src/probe/branches.test.ts": TESTED_ONE_BRANCH },
    before: [coverageRun()],
    run: coverageRatchet(),
    expect: ["below the baseline"],
  },
  {
    // The other way round: the same file with both branches tested keeps the report at the
    // baseline, so the probe above fails on the untested branch and on nothing else.
    gate: "Vitest + coverage ratchet (every branch tested)",
    files: { ...BRANCHES, "src/probe/branches.test.ts": TESTED_BOTH_BRANCHES },
    before: [coverageRun()],
    run: coverageRatchet(),
    accept: true,
    expect: [],
  },
  {
    // A new file no test loads must still count as uncovered: that is what `coverage.include` of
    // vitest.config.ts is for, and the probes above override it.
    gate: "Vitest coverage include (a file no test loads)",
    files: {
      "src/probe/unloaded.ts": "export const unloaded = (): number => {\n  return 1;\n};\n",
      "src/probe/other.test.ts":
        'import { expect, it } from "vitest";\n\nit("runs", () => {\n  expect(1).toBe(1);\n});\n',
    },
    before: [projectCoverageRun()],
    run: unloadedFileListed(),
    accept: true,
    expect: [],
  },
  {
    gate: "size-limit",
    files: {},
    edits: {
      ".size-limit.mjs": (text) => text.replace(/limit: "[^"]*"/g, 'limit: "1 B"'),
    },
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
 * Copy ui/ and the built interface into the scratch root, node_modules linked.
 */
function prepareScratch() {
  if (!existsSync(join(REPO, DIST, "index.html"))) {
    console.error(`No build in ${DIST}: run \`npm --prefix ui run build\` first.`);
    process.exit(1);
  }
  const inside = relative(REPO, SCRATCH);
  if (!(isAbsolute(inside) || inside === ".." || inside.startsWith(`..${sep}`))) {
    console.error(
      `GATE_PROBE_DIR (${SCRATCH}) is inside the repository; choose a scratch directory outside it.`,
    );
    process.exit(1);
  }
  if (existsSync(SCRATCH) && readdirSync(SCRATCH).length > 0 && !existsSync(MARKER)) {
    console.error(
      `GATE_PROBE_DIR (${SCRATCH}) is not empty and was not made by this script; choose an empty or new directory.`,
    );
    process.exit(1);
  }
  for (const made of [
    PROBE_UI,
    join(SCRATCH, DIST),
    join(SCRATCH, "packages/lakana-core"),
    JSCPD_REPORT,
  ]) {
    rmSync(made, { recursive: true, force: true });
  }
  mkdirSync(SCRATCH, { recursive: true });
  writeFileSync(MARKER, "Scratch copy of ui/scripts/prove-gates.mjs; safe to delete.\n");
  cpSync(UI, PROBE_UI, {
    recursive: true,
    filter: (source) => !NOT_COPIED.has(relative(UI, source).split("/")[0] ?? ""),
  });
  cpSync(join(REPO, DIST), join(SCRATCH, DIST), { recursive: true });
  symlinkSync(join(UI, "node_modules"), join(PROBE_UI, "node_modules"), "dir");
  for (const source of SERVER_SOURCES) {
    mkdirSync(dirname(join(SCRATCH, source)), { recursive: true });
    symlinkSync(join(REPO, source), join(SCRATCH, source), "file");
  }
}

/**
 * Write a probe's violation into the scratch copy.
 *
 * @param {Probe} probe - the probe
 * @returns {() => void} what undoes it
 */
function plant(probe) {
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
  return () => {
    for (const path of Object.keys(probe.files)) {
      rmSync(join(PROBE_UI, path), { force: true });
    }
    for (const [path, text] of Object.entries(originals)) {
      writeFileSync(join(PROBE_UI, path), text);
    }
  };
}

/**
 * Run the steps before a probe's gate, whatever their exit, and keep those that failed.
 *
 * @param {Probe} probe - the probe
 * @returns {{label: string, status: number | null, output: string}[]} the failed steps
 */
function runBefore(probe) {
  return (probe.before ?? []).flatMap((command) => {
    const { status, output } = execute(command);
    const label = [command.program.split(sep).at(-1) ?? "", ...command.args.slice(0, 3)].join(" ");
    return status === 0 ? [] : [{ label, status, output }];
  });
}

/**
 * Run one probe in the scratch copy and undo its changes.
 *
 * @param {Probe} probe - the violation and the gate
 * @returns {string | null} why the gate let the violation through, or null when it caught it
 */
function prove(probe) {
  const undo = plant(probe);
  try {
    const failedBefore = runBefore(probe);
    const { status, output } = execute(probe.run);
    return verdict(probe, { status, output, shared: false, failedBefore }, TIMEOUT_MS / 1000);
  } finally {
    undo();
  }
}

/**
 * Run the ESLint probes in one ESLint run and judge each by the lines about its own files.
 *
 * @param {Probe[]} probes - the ESLint probes, whose files do not overlap
 * @returns {(string | null)[]} each probe's leak, or null when ESLint caught it
 */
function proveTogether(probes) {
  const paths = probes.flatMap((probe) => Object.keys(probe.files));
  if (new Set(paths).size !== paths.length || probes.some((p) => p.edits || p.before)) {
    console.error("The ESLint probes must write distinct files and edit nothing.");
    process.exit(1);
  }
  const undos = probes.map(plant);
  try {
    const { status, output } = execute(npmRun("lint"));
    const sections = sectionsByFile(output);
    // An ESLint run that crashed reports no file: show its output rather than a silent pass.
    const own = (/** @type {Probe} */ probe) =>
      sections.size === 0
        ? output
        : Object.keys(probe.files)
            .map((path) => sections.get(join(PROBE_UI, path)) ?? "")
            .join("");
    return probes.map((probe) =>
      verdict(
        probe,
        { status, output: own(probe), shared: true, failedBefore: [] },
        TIMEOUT_MS / 1000,
      ),
    );
  } finally {
    for (const undo of undos) {
      undo();
    }
  }
}

prepareScratch();
const leaks = [];
const started = Date.now();
const together = PROBES.filter((probe) => probe.eslint === true);
const sharedLeaks = proveTogether(together);
for (const probe of PROBES) {
  const leak =
    probe.eslint === true ? (sharedLeaks[together.indexOf(probe)] ?? null) : prove(probe);
  if (leak === null && probe.accept === true) {
    console.log(`ok   ${probe.gate}: accepted its allowed file, as it should`);
  } else if (leak === null) {
    console.log(
      `ok   ${probe.gate}: failed on its violation, as it should (${[...new Set(probe.expect)].join(", ")})`,
    );
  } else {
    console.log(`LEAK ${probe.gate}: wrong verdict, ${leak}`);
    leaks.push(probe.gate);
  }
}
if (leaks.length > 0) {
  console.error(
    `\nThese gates no longer judge their probe right: ${leaks.join(", ")}. Fix the gate, not the probe.`,
  );
  process.exit(1);
}
const seconds = String(Math.round((Date.now() - started) / 1000));
console.log(
  `\nAll ${String(PROBES.length)} gate probes got the verdict they should (${seconds} s).`,
);
