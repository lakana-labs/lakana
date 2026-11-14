// The verdicts of ui/scripts/prove-gates.mjs, apart from its file system work so Vitest can test
// them (scripts/gate-verdict.test.ts).

/**
 * @typedef {object} Outcome
 * @property {number | null} status - the gate's exit status (null on a timeout)
 * @property {string} output - what it printed, or for a shared run the part about the probe's files
 * @property {boolean} shared - whether the run held other probes' violations too, so its exit
 *   status is theirs as much as this probe's
 * @property {{label: string, status: number | null, output: string}[]} failedBefore - the steps
 *   before the gate that did not exit 0, with their output
 */

/** How much of an output a message quotes, from its end. */
const TAIL = 600;

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
 * Split ESLint's default ("stylish") output into the part about each file.
 *
 * Stylish prints a file's absolute path on a line of its own, its problems indented below it,
 * and a blank line after them.
 *
 * @param {string} output - ESLint's output
 * @returns {Map<string, string>} each file's absolute path and the lines about it
 */
export function sectionsByFile(output) {
  /** @type {Map<string, string>} */
  const sections = new Map();
  /** @type {string | null} */
  let current = null;
  for (const line of output.split("\n")) {
    if (line.startsWith("/")) {
      current = line.trimEnd();
      sections.set(current, "");
    } else if (current !== null && /^\s+\S/.test(line)) {
      sections.set(current, `${sections.get(current) ?? ""}${line}\n`);
    } else {
      current = null;
    }
  }
  return sections;
}

/**
 * Judge a gate's run against a probe.
 *
 * @param {{expect: string[], accept?: boolean}} probe - the texts the run must print, twice when
 *   listed twice; with `accept`, the gate must pass instead
 * @param {Outcome} outcome - how the gate's run went
 * @param {number} timeoutSeconds - the time a gate gets
 * @returns {string | null} why the gate let the violation through, or null when it caught it
 */
export function verdict(probe, outcome, timeoutSeconds) {
  const leak = judge(probe, outcome, timeoutSeconds);
  if (leak === null) {
    return null;
  }
  // A step before the gate that failed (say the coverage run) is usually why the gate's own
  // output makes no sense (a missing report): show its end too.
  const before = outcome.failedBefore.map(
    (step) =>
      `; the step before it, \`${step.label}\`, exited ${String(step.status)}: ${step.output.slice(-TAIL)}`,
  );
  return `${leak}${before.join("")}`;
}

/**
 * Judge a gate's run, without the steps before it.
 *
 * @param {{expect: string[], accept?: boolean}} probe - the expectations
 * @param {Outcome} outcome - how the gate's run went
 * @param {number} timeoutSeconds - the time a gate gets
 * @returns {string | null} the leak, or null
 */
function judge(probe, outcome, timeoutSeconds) {
  const { status, output } = outcome;
  if (probe.accept === true) {
    // A shared run fails on the other probes' files: here only a word about this one counts.
    const accepted = outcome.shared ? status !== null && output.trim() === "" : status === 0;
    return accepted
      ? null
      : `it refused an allowed file (exit ${String(status)}): ${output.slice(-TAIL)}`;
  }
  if (status === 0) {
    return "it exited 0";
  }
  if (status === null) {
    return `it did not finish within ${String(timeoutSeconds)} s: ${output.slice(-300)}`;
  }
  for (const text of new Set(probe.expect)) {
    const wanted = occurrences(probe.expect.join("\n"), text);
    if (occurrences(output, text) < wanted) {
      return `it failed, but its output lacks "${text}" (${String(wanted)}x): ${output.slice(-TAIL)}`;
    }
  }
  return null;
}
