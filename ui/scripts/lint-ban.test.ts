// The ban on HTML sinks (docs/agent/security.md, Preview and Defined in) is a lint rule, and a
// rule that never fires guards nothing: each fixture holds one form of raw HTML, and the project's
// own config must refuse each with exactly one error.
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

const UI = join(dirname(fileURLToPath(import.meta.url)), "..");
const FIXTURES = join(UI, "scripts", "lint-fixtures");
const MESSAGE = "Render text nodes only (docs/agent/security.md, Preview and Defined in)";
const FORMS = [
  "jsx-attribute.tsx",
  "object-property.tsx",
  "inner-html.tsx",
  "outer-html.tsx",
  "insert-adjacent.tsx",
  "jsx-srcdoc.tsx",
  "jsx-srcdoc-lowercase.tsx",
  "property-name.tsx",
  "property-quoted.tsx",
  "computed-assignment.tsx",
  "contextual-fragment.tsx",
  "parse-from-string.tsx",
  "set-html-unsafe.tsx",
  "document-write.tsx",
  "document-writeln.tsx",
];

/**
 * Lints fixtures with the project's config; the fixtures are ignored by the normal run.
 *
 * @param files - The fixtures' file names.
 * @returns Each file's messages, by file name.
 */
async function lint(files: readonly string[]): Promise<Map<string, ESLint.LintResult>> {
  const eslint = new ESLint({ cwd: UI, ignore: false });
  const results = await eslint.lintFiles(files.map((file) => join(FIXTURES, file)));
  return new Map(results.map((result) => [result.filePath.slice(FIXTURES.length + 1), result]));
}

describe("the ban on raw HTML", () => {
  it("fails the lint on each form of raw HTML", { timeout: 120_000 }, async () => {
    const results = await lint([...FORMS, "allowed.tsx"]);

    for (const form of FORMS) {
      const messages = results.get(form)?.messages.map((message) => message.message);
      expect(messages, form).toEqual([MESSAGE]);
    }
    expect(results.get("allowed.tsx")?.messages).toEqual([]);
  });

  it("keeps the fixtures out of the normal lint run", { timeout: 120_000 }, async () => {
    const eslint = new ESLint({ cwd: UI });

    expect(await eslint.isPathIgnored(join(FIXTURES, "inner-html.tsx"))).toBe(true);
  });
});
