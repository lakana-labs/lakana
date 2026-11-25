// The ban on HTML sinks (docs/agent/security.md, Preview and Defined in) and the ban on text not
// taken from i18next (docs/agent/conventions.md, "Every string the user sees goes through i18next")
// are lint rules, and a rule that never fires guards nothing: each fixture holds one form, and the
// project's own config must refuse each with exactly one error.
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
const TEXT_MESSAGE =
  'Text a person reads comes from i18next: put it in a message catalogue and use t() (docs/agent/conventions.md, "Every string the user sees goes through i18next")';
const TEXT_FORMS = [
  "text-jsx.tsx",
  "text-string.tsx",
  "text-template.tsx",
  "text-conditional.tsx",
  "text-aria-label.tsx",
  "text-title.tsx",
  "text-placeholder.tsx",
  "text-alt.tsx",
  "text-attribute-template.tsx",
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

describe("the ban on text not taken from i18next", () => {
  it("fails the lint on each form of literal text", { timeout: 120_000 }, async () => {
    const results = await lint([...TEXT_FORMS, "text-allowed.tsx"]);

    for (const form of TEXT_FORMS) {
      const messages = results.get(form)?.messages.map((message) => message.message);
      expect(messages, form).toEqual([TEXT_MESSAGE]);
    }
    expect(results.get("text-allowed.tsx")?.messages).toEqual([]);
  });

  it("leaves tests alone", { timeout: 120_000 }, async () => {
    const eslint = new ESLint({ cwd: UI });
    const config = (await eslint.calculateConfigForFile(join(UI, "src", "App.test.tsx"))) as {
      rules?: Record<string, unknown>;
    };

    expect(config.rules?.["lakana/no-literal-text"]).toBeUndefined();
  });
});
