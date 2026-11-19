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
import type { Completion, CompletionSource } from "@codemirror/autocomplete";
import type { Text } from "@codemirror/state";

import type { ColumnOut } from "../../api/table";
import { markedText } from "../../shell/VisibleText";
import type { CompletionCatalog } from "./catalog";

// A word of a name, unquoted.
const WORD = String.raw`[\p{L}\p{N}_$]+`;
// A dotted path up to the cursor: its qualifiers and the word typed so far.
const PATH_BEFORE = new RegExp(String.raw`(?:${WORD}\.)*[\p{L}\p{N}_$]*`, "u");
// FROM or JOIN right before the word.
const AFTER_FROM = /\b(?:from|join)\s+$/i;
// A table after FROM or JOIN right before the word, which is then the table's alias.
const AFTER_TABLE = new RegExp(
  String.raw`\b(?:from|join)\s+(?:${WORD}\.)+${WORD}\s+(?:as\s+)?$`,
  "iu",
);
// Words that may follow a table in FROM and are not its alias.
const NOT_ALIAS = [
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
];
// A table after FROM or JOIN, with its alias if it has one. A keyword after the table is no alias,
// and is left for the next match (the JOIN of the next table).
const TABLE_IN_FROM = new RegExp(
  String.raw`\b(?:from|join)\s+((?:${WORD}\.)+${WORD})(?:\s+(?:as\s+)?(?!(?:${NOT_ALIAS.join("|")})(?![\p{L}\p{N}_$]))(${WORD}))?`,
  "giu",
);

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

/** A table a statement reads, and its alias there. */
interface TableRead {
  /** The table. */
  readonly at: TableAt;
  /** Its alias, or null when it has none. */
  readonly alias: string | null;
  /** What the statement calls it: its alias, or else its whole name. */
  readonly qualifier: string;
}

/**
 * The tables a statement reads after FROM and JOIN, in their order, each once by what the
 * statement calls it.
 *
 * @param statement - The statement.
 * @returns The tables.
 */
function tablesRead(statement: string): TableRead[] {
  const found = new Map<string, TableRead>();
  for (const match of statement.matchAll(TABLE_IN_FROM)) {
    const whole = match[1] as string;
    const parts = whole.split(".");
    const table = parts.pop() as string;
    const alias = match[2] ?? null;
    const qualifier = alias ?? whole;
    if (!found.has(qualifier.toLowerCase())) {
      found.set(qualifier.toLowerCase(), {
        at: { namespace: parts.join("."), table },
        alias,
        qualifier,
      });
    }
  }
  return [...found.values()];
}

/**
 * The tables a statement reads, by their aliases and their own names, in lower case.
 *
 * @param statement - The statement.
 * @returns Each name and its table.
 */
function tablesIn(statement: string): Map<string, TableAt> {
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
 * @param statement - The statement.
 * @returns Each table with its columns, in the statement's order.
 */
export async function columnsRead(
  catalog: CompletionCatalog,
  statement: string,
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
async function bareColumns(catalog: CompletionCatalog, statement: string): Promise<Completion[]> {
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
  return async (context) => {
    // The pattern matches the empty string too, so there is always a match.
    const path = context.matchBefore(PATH_BEFORE) as { from: number; text: string };
    const qualifier = path.text.split(".");
    const word = qualifier.pop() as string;
    const from = context.pos - word.length;
    if (qualifier.length === 0) {
      const before = context.state.sliceDoc(0, path.from);
      if (AFTER_FROM.test(before)) {
        const top = childNamespaces(await catalog.namespaces(), "");
        return { from, options: top.map((part) => named(part, "namespace")) };
      }
      // A table's alias is the person's to write; a space alone asks nothing. The language's
      // keywords answer beside the columns.
      if (AFTER_TABLE.test(before) || (word === "" && !context.explicit)) {
        return null;
      }
      const options = await bareColumns(catalog, statementAt(context.state.doc, context.pos));
      return options.length === 0 ? null : { from, options };
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
      options.push(columnOption(column, markedText(column.type)));
    }
    return options.length === 0 ? null : { from, options };
  };
}
