// The rule watches attributes by name, so the list must keep up with the interface: every prop of
// ui/src that is given t() text is either watched or named as not read, with the reason.
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { expect, it } from "vitest";

import { NOT_READ, READ_ATTRIBUTES } from "./no-literal-text.ts";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

/**
 * The components of `ui/src`, tests left out.
 *
 * @param directory - Where to look.
 * @returns The `.tsx` files.
 */
function components(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return entry.name === "test" ? [] : components(path);
    return entry.name.endsWith(".tsx") && !entry.name.endsWith(".test.tsx") ? [path] : [];
  });
}

/**
 * Whether an expression is text from `t()`: a call, or a branch, join or template of them.
 *
 * @param node - The attribute's expression.
 * @returns True when its value is translated text.
 */
function isTranslated(node: ts.Expression): boolean {
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)) {
    return isTranslated(node.expression);
  }
  if (ts.isCallExpression(node)) {
    const callee = node.expression;
    return (
      (ts.isIdentifier(callee) && callee.text === "t") ||
      (ts.isPropertyAccessExpression(callee) && callee.name.text === "t")
    );
  }
  if (ts.isConditionalExpression(node)) {
    return isTranslated(node.whenTrue) || isTranslated(node.whenFalse);
  }
  if (ts.isBinaryExpression(node)) return isTranslated(node.left) || isTranslated(node.right);
  if (ts.isTemplateExpression(node)) {
    return node.templateSpans.some((span) => isTranslated(span.expression));
  }
  return false;
}

/**
 * Every attribute given `t()` text, as `Element.attribute`.
 *
 * @param file - The component's file.
 * @returns The attributes.
 */
function translatedAttributes(file: string): string[] {
  const source = ts.createSourceFile(
    file,
    readFileSync(file, "utf8"),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const found: string[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isJsxAttribute(node) &&
      node.initializer !== undefined &&
      ts.isJsxExpression(node.initializer) &&
      node.initializer.expression !== undefined &&
      isTranslated(node.initializer.expression)
    ) {
      found.push(`${node.parent.parent.tagName.getText()}.${node.name.getText()}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

it("watches every prop the interface gives translated text, or says why it is not read", () => {
  const given = new Set(components(SRC).flatMap(translatedAttributes));
  const unknown = [...given].filter((attribute) => {
    const name = attribute.slice(attribute.lastIndexOf(".") + 1);
    return !READ_ATTRIBUTES.has(name) && !(attribute in NOT_READ);
  });

  expect(given.size).toBeGreaterThan(40);
  expect(unknown).toEqual([]);
});

it("gives a reason for every prop it does not read", () => {
  for (const [attribute, reason] of Object.entries(NOT_READ)) {
    expect(attribute, attribute).toMatch(/^[A-Z][\w.]*\.[\w-]+$/);
    expect(reason.length, attribute).toBeGreaterThan(20);
  }
});
