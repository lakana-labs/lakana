import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { CONTENT_SECURITY_POLICY, LIBRARY_STYLE_HASHES } from "../csp";

const MODULES = resolve(import.meta.dirname, "../node_modules");
// The shipped libraries that could insert a <style> element at run time.
const LIBRARIES = ["react-aria", "react-aria-components", "react-stately", "react-router"];
const STYLE_ELEMENT = /createElement\(\s*['"`]style['"`]\s*\)/g;

/**
 * Lists every ES module file below a directory.
 *
 * @param directory - Where to start.
 * @returns The `.mjs` and `.js` files' paths.
 */
function modules(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true, recursive: true })
    .filter((entry) => entry.isFile() && /\.m?js$/.test(entry.name))
    .map((entry) => join(entry.parentPath, entry.name));
}

/**
 * Reads the text a library's `style.textContent = \`...\`.trim()` sets, its `${constant}`
 * interpolations resolved from the same file.
 *
 * @param source - The module's source.
 * @returns The style texts in the order they appear.
 */
function styleTexts(source: string): string[] {
  return [...source.matchAll(/\.textContent\s*=\s*`([^`]*)`\.trim\(\)/g)].map((match) =>
    (match[1] ?? "")
      .replace(/\$\{([^}]+)\}/g, (_whole, name: string) => {
        const constant = new RegExp(`const ${name.replace(/\$/g, "\\$")}\\s*=\\s*'([^']*)'`).exec(
          source,
        );
        if (constant === null) {
          throw new Error(`cannot resolve \${${name}} in a style text`);
        }
        return constant[1] ?? "";
      })
      .trim(),
  );
}

const files = LIBRARIES.flatMap((library) => modules(join(MODULES, library, "dist"))).filter(
  (file) => file.endsWith(".mjs"),
);
const injecting = files.filter((file) => readFileSync(file, "utf8").match(STYLE_ELEMENT) !== null);

describe("the content security policy and the libraries' own styles", () => {
  it("lists one hash per place a library inserts a <style>", () => {
    const sites = injecting.reduce(
      (count, file) => count + (readFileSync(file, "utf8").match(STYLE_ELEMENT) ?? []).length,
      0,
    );

    expect(sites, injecting.join("\n")).toBe(LIBRARY_STYLE_HASHES.length);
  });

  it("allows exactly the text each of them inserts", () => {
    const texts = injecting.flatMap((file) => styleTexts(readFileSync(file, "utf8")));
    expect(texts).toHaveLength(LIBRARY_STYLE_HASHES.length);

    for (const text of texts) {
      const hash = `'sha256-${createHash("sha256").update(text).digest("base64")}'`;
      expect(LIBRARY_STYLE_HASHES, text).toContain(hash);
    }
  });

  it("puts the hashes into style-src and nothing looser", () => {
    const styleSource = CONTENT_SECURITY_POLICY.split("; ").find((directive) =>
      directive.startsWith("style-src "),
    );

    expect(styleSource).toBe(`style-src 'self' ${LIBRARY_STYLE_HASHES.join(" ")}`);
    expect(CONTENT_SECURITY_POLICY).not.toContain("unsafe-");
  });
});
