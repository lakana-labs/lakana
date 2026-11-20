/**
 * Completion of catalog names in the SQL editor: namespaces, tables and columns.
 *
 * @remarks
 * The SQL keywords come from the language (`@codemirror/lang-sql`); this source adds the names
 * the person may read, from {@link CompletionCatalog}: a word without a table is answered with the
 * columns of the tables the statement reads. A name is inserted as it is and shown with
 * `markedText`, so a hidden character shows as `U+XXXX`; a column's description is a text node.
 * @packageDocumentation
 */
import { type Completion, type CompletionSource, ifNotIn } from "@codemirror/autocomplete";

import type { ColumnOut } from "../../api/table";
import { markedText } from "../../shell/VisibleText";
import type { CompletionCatalog } from "./catalog";
import {
  placeAfter,
  type Statement,
  statementAt,
  type TableAt,
  type TableRead,
  tablesRead,
} from "./fromClause";

// A dotted path up to the cursor: its qualifiers and the word typed so far.
const PATH_BEFORE = /(?:[\p{L}\p{N}_$]+\.)*[\p{L}\p{N}_$]*/u;
// Where a name is text, not SQL: the catalog offers nothing there, as the language's keywords do
// not.
const NOT_SQL = ["QuotedIdentifier", "String", "LineComment", "BlockComment"];

/**
 * The tables in scope, by their aliases and their own names, in lower case.
 *
 * @param statement - The statement and the position.
 * @returns Each name and its table.
 */
function tablesIn(statement: Statement): Map<string, TableAt> {
  const found = new Map<string, TableAt>();
  for (const { at, alias } of tablesRead(statement)) {
    found.set(at.table.toLowerCase(), at);
    if (alias !== null) {
      found.set(alias.toLowerCase(), at);
    }
  }
  return found;
}

/** A table a statement reads, with its columns. */
export interface ColumnsRead {
  /** The table. */
  readonly read: TableRead;
  /** Its columns. */
  readonly columns: readonly ColumnOut[];
}

/**
 * The columns of the tables a statement reads, of those whose columns can be read.
 *
 * @param catalog - The environment's catalog.
 * @param statement - The statement and the position.
 * @returns Each table in scope with its columns, in the statement's order.
 */
export async function columnsRead(
  catalog: CompletionCatalog,
  statement: Statement,
): Promise<ColumnsRead[]> {
  const reads = tablesRead(statement);
  const columns = await Promise.all(reads.map(({ at }) => catalog.columns(at.namespace, at.table)));
  return reads.flatMap((read, index) => {
    const found = columns[index];
    return found === null || found === undefined ? [] : [{ read, columns: found }];
  });
}

/**
 * The table a qualifier names: `namespace.table` when the namespace is known and the whole
 * qualifier is not a namespace too (then it names a namespace, and no table is read), or an alias
 * or a table's own name in the statement.
 *
 * @param qualifier - The parts before the column.
 * @param namespaces - The known namespaces.
 * @param statement - The statement and the position.
 * @returns The table, or null.
 */
export function tableOf(
  qualifier: readonly string[],
  namespaces: readonly string[],
  statement: Statement,
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
 * An option for a column: its type, and its description as text when it has one.
 *
 * @param column - The column.
 * @param detail - What is shown beside the name: the column's type, after its table's name when
 *   several tables have the name.
 * @returns The option.
 */
function columnOption(column: ColumnOut, detail: string): Completion {
  const { description } = column;
  return {
    ...named(column.name, "property"),
    detail,
    ...(description === null
      ? {}
      : { info: () => document.createTextNode(markedText(description)) }),
  };
}

/**
 * The columns of the tables a statement reads, for a word without its table. A name that several
 * of them have is offered once for each, named and inserted with what the statement calls its
 * table, since the name alone would be ambiguous.
 *
 * @param catalog - The environment's catalog.
 * @param statement - The statement.
 * @returns The options.
 */
async function bareColumns(
  catalog: CompletionCatalog,
  statement: Statement,
): Promise<Completion[]> {
  const read = await columnsRead(catalog, statement);
  const tablesOf = new Map<string, number>();
  for (const { columns } of read) {
    for (const name of new Set(columns.map((column) => column.name))) {
      tablesOf.set(name, (tablesOf.get(name) ?? 0) + 1);
    }
  }
  return read.flatMap(({ read: { qualifier }, columns }) =>
    columns.map((column) => {
      const type = markedText(column.type);
      return (tablesOf.get(column.name) as number) > 1
        ? {
            ...columnOption(column, `${markedText(qualifier)} · ${type}`),
            apply: `${qualifier}.${column.name}`,
          }
        : columnOption(column, type);
    }),
  );
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
 *   its path and a dot, a table's columns, with their types and descriptions, after its alias,
 *   its name or its whole name and a dot, and the columns of the statement's tables for a word
 *   elsewhere.
 */
export function catalogCompletion(catalog: CompletionCatalog): CompletionSource {
  return ifNotIn(NOT_SQL, async (context) => {
    // The pattern matches the empty string too, so there is always a match.
    const path = context.matchBefore(PATH_BEFORE) as { from: number; text: string };
    const qualifier = path.text.split(".");
    const word = qualifier.pop() as string;
    const from = context.pos - word.length;
    const statement = statementAt(context.state.doc, context.pos);
    if (qualifier.length === 0) {
      const place = placeAfter(statement.text.slice(0, statement.at - path.text.length));
      if (place === "table") {
        const top = childNamespaces(await catalog.namespaces(), "");
        return { from, options: top.map((part) => named(part, "namespace")) };
      }
      // A table's alias is the person's to write; a space alone asks nothing. The language's
      // keywords answer beside the columns.
      if (place === "alias" || (word === "" && !context.explicit)) {
        return null;
      }
      const options = await bareColumns(catalog, statement);
      return options.length === 0 ? null : { from, options };
    }
    const namespaces = await catalog.namespaces();
    const below = qualifier.join(".");
    const options = childNamespaces(namespaces, below).map((part) => named(part, "namespace"));
    if (namespaces.includes(below)) {
      const tables = await catalog.tables(below, word);
      options.push(...tables.map((table) => named(table, "class")));
    }
    const target = tableOf(qualifier, namespaces, statement);
    const columns = target === null ? null : await catalog.columns(target.namespace, target.table);
    for (const column of columns ?? []) {
      options.push(columnOption(column, markedText(column.type)));
    }
    return options.length === 0 ? null : { from, options };
  });
}
