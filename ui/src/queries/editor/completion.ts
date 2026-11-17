/**
 * Completion of catalog names in the SQL editor: namespaces, tables and columns.
 *
 * @remarks
 * The SQL keywords come from the language (`@codemirror/lang-sql`); this source adds the names
 * the person may read, from {@link CompletionCatalog}. A name is inserted as it is and shown with
 * `markedText`, so a hidden character shows as `U+XXXX`; a column's description is a text node.
 * @packageDocumentation
 */
import type { Completion, CompletionSource } from "@codemirror/autocomplete";
import type { Text } from "@codemirror/state";

import { markedText } from "../../shell/VisibleText";
import type { CompletionCatalog } from "./catalog";

// A word of a name, unquoted.
const WORD = String.raw`[\p{L}\p{N}_$]+`;
// A dotted path up to the cursor: its qualifiers and the word typed so far.
const PATH_BEFORE = new RegExp(String.raw`(?:${WORD}\.)*[\p{L}\p{N}_$]*`, "u");
// FROM or JOIN right before the word.
const AFTER_FROM = /\b(?:from|join)\s+$/i;
// A table after FROM or JOIN, with its alias if it has one.
const TABLE_IN_FROM = new RegExp(
  String.raw`\b(?:from|join)\s+((?:${WORD}\.)+${WORD})(?:\s+(?:as\s+)?(${WORD}))?`,
  "giu",
);
// Words that may follow a table in FROM and are not its alias.
const NOT_ALIAS = new Set([
  "anti",
  "cross",
  "except",
  "full",
  "group",
  "having",
  "inner",
  "intersect",
  "join",
  "left",
  "limit",
  "natural",
  "on",
  "order",
  "outer",
  "positional",
  "qualify",
  "right",
  "semi",
  "union",
  "using",
  "where",
  "window",
]);

/** A table, by its namespace and name. */
export interface TableAt {
  /** The dotted namespace. */
  readonly namespace: string;
  /** The table's name. */
  readonly table: string;
}

/**
 * The statement the position is in: the text between the semicolons around it.
 *
 * @param doc - The document.
 * @param pos - The position.
 * @returns The statement's text.
 */
export function statementAt(doc: Text, pos: number): string {
  const text = doc.toString();
  const end = text.indexOf(";", pos);
  return text.slice(text.lastIndexOf(";", pos - 1) + 1, end < 0 ? text.length : end);
}

/**
 * The tables a statement reads, by their aliases and their own names, in lower case.
 *
 * @param statement - The statement.
 * @returns Each name and its table.
 */
function tablesIn(statement: string): Map<string, TableAt> {
  const found = new Map<string, TableAt>();
  for (const match of statement.matchAll(TABLE_IN_FROM)) {
    const parts = (match[1] as string).split(".");
    const alias = match[2];
    const table = parts.pop() as string;
    const at = { namespace: parts.join("."), table };
    found.set(table.toLowerCase(), at);
    if (alias !== undefined && !NOT_ALIAS.has(alias.toLowerCase())) {
      found.set(alias.toLowerCase(), at);
    }
  }
  return found;
}

/**
 * The table a qualifier names: `namespace.table` when the namespace is known and the whole
 * qualifier is not a namespace too (then it names a namespace, and no table is read), or an alias
 * or a table's own name in the statement.
 *
 * @param qualifier - The parts before the column.
 * @param namespaces - The known namespaces.
 * @param statement - The statement.
 * @returns The table, or null.
 */
export function tableOf(
  qualifier: readonly string[],
  namespaces: readonly string[],
  statement: string,
): TableAt | null {
  if (qualifier.length === 1) {
    return tablesIn(statement).get((qualifier[0] as string).toLowerCase()) ?? null;
  }
  // A qualifier that is itself a namespace names no table: what follows it is a table.
  const namespace = qualifier.slice(0, -1).join(".");
  return namespaces.includes(namespace) && !namespaces.includes(qualifier.join("."))
    ? { namespace, table: qualifier[qualifier.length - 1] as string }
    : null;
}

/**
 * An option for a name: inserted as it is, shown with its hidden characters marked.
 *
 * @param name - The name.
 * @param type - What it is, which picks the option's icon.
 * @returns The option.
 */
function named(name: string, type: string): Completion {
  return { label: name, displayLabel: markedText(name), type };
}

/**
 * The next part of each namespace below a path, once each.
 *
 * @param namespaces - The namespaces.
 * @param below - The path, or empty for the top.
 * @returns The parts.
 */
function childNamespaces(namespaces: readonly string[], below: string): string[] {
  const start = below === "" ? "" : `${below}.`;
  const parts = namespaces
    .filter((path) => path.startsWith(start))
    .map((path) => path.slice(start.length).split(".")[0] as string);
  return [...new Set(parts)];
}

/**
 * The completion source of the catalog.
 *
 * @param catalog - The environment's catalog.
 * @returns The source: namespaces after FROM or JOIN, a namespace's namespaces and tables after
 *   its path and a dot, and a table's columns, with their types and descriptions, after its
 *   alias, its name or its whole name and a dot.
 */
export function catalogCompletion(catalog: CompletionCatalog): CompletionSource {
  return async (context) => {
    // The pattern matches the empty string too, so there is always a match.
    const path = context.matchBefore(PATH_BEFORE) as { from: number; text: string };
    const qualifier = path.text.split(".");
    const word = qualifier.pop() as string;
    const from = context.pos - word.length;
    if (qualifier.length === 0) {
      // A word elsewhere is SQL's, and the language's keywords answer it.
      if (!AFTER_FROM.test(context.state.sliceDoc(0, path.from))) {
        return null;
      }
      const top = childNamespaces(await catalog.namespaces(), "");
      return { from, options: top.map((part) => named(part, "namespace")) };
    }
    const namespaces = await catalog.namespaces();
    const below = qualifier.join(".");
    const options = childNamespaces(namespaces, below).map((part) => named(part, "namespace"));
    if (namespaces.includes(below)) {
      const tables = await catalog.tables(below, word);
      options.push(...tables.map((table) => named(table, "class")));
    }
    const target = tableOf(qualifier, namespaces, statementAt(context.state.doc, context.pos));
    const columns = target === null ? null : await catalog.columns(target.namespace, target.table);
    for (const column of columns ?? []) {
      const { description } = column;
      options.push({
        ...named(column.name, "property"),
        detail: markedText(column.type),
        ...(description === null
          ? {}
          : { info: () => document.createTextNode(markedText(description)) }),
      });
    }
    return options.length === 0 ? null : { from, options };
  };
}
