/**
 * The tables a SQL statement reads, as the editor's help needs them: after FROM, JOIN and the
 * commas of a FROM list, each with its alias and the parentheses it sits in.
 *
 * @remarks
 * Not a parser of SQL, but enough of one for completion and hover. Comments, strings and quoted
 * identifiers are read as blanks. A FROM counts as a clause only where it can start one: in a
 * group (the statement, or the inside of a parenthesis) that has a SELECT before it, as the
 * group's first word (DuckDB's FROM-first) or after DELETE; so `extract(year FROM ts)` and
 * `IS DISTINCT FROM` read no table. A table counts where the cursor is when its FROM sits in the
 * cursor's group or one around it, so a subquery's or a CTE's tables stay inside their
 * parentheses.
 * @packageDocumentation
 */
import type { Text } from "@codemirror/state";

// A word of a name, unquoted.
const WORD = String.raw`[\p{L}\p{N}_$]+`;
// A dotted path, a parenthesis, a comma, or any other mark.
const TOKEN = new RegExp(String.raw`(?:${WORD}\.)*${WORD}|[^\s\p{L}\p{N}_$]`, "gu");
const SINGLE_WORD = new RegExp(String.raw`^${WORD}$`, "u");
const DOTTED = new RegExp(String.raw`^(?:${WORD}\.)+${WORD}$`, "u");
// What is read as blanks: comments, strings and quoted identifiers, to their end or the text's.
const BLANKS = /--[^\n]*|\/\*[\s\S]*?(?:\*\/|$)|'(?:[^']|'')*(?:'|$)|"(?:[^"]|"")*(?:"|$)/g;

// Words that may follow a table in FROM and are not its alias.
const NOT_ALIAS = new Set([
  "anti",
  "asof",
  "cross",
  "except",
  "full",
  "group",
  "having",
  "inner",
  "intersect",
  "join",
  "lateral",
  "left",
  "limit",
  "natural",
  "offset",
  "on",
  "order",
  "outer",
  "pivot",
  "positional",
  "qualify",
  "returning",
  "right",
  "select",
  "semi",
  "tablesample",
  "union",
  "unpivot",
  "using",
  "where",
  "window",
]);
// Words that end a FROM clause in its group.
const FROM_ENDS = new Set([
  "except",
  "group",
  "having",
  "intersect",
  "limit",
  "offset",
  "order",
  "pivot",
  "qualify",
  "returning",
  "select",
  "union",
  "unpivot",
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

/** A table a statement reads, and its alias there. */
export interface TableRead {
  /** The table. */
  readonly at: TableAt;
  /** Its alias, or null when it has none. */
  readonly alias: string | null;
  /** What the statement calls it: its alias, or else its whole name. */
  readonly qualifier: string;
}

/** The statement a position is in, and where the position is in it. */
export interface Statement {
  /** The text between the semicolons around the position. */
  readonly text: string;
  /** The position, counted from the statement's start. */
  readonly at: number;
}

/** What the cursor is expected to write next, from the text before it. */
export type Place = "table" | "alias" | "other";

/** A table found, with the group its FROM is in. */
interface Found {
  /** The table, its alias set once read. */
  read: { at: TableAt; alias: string | null };
  /** The group. */
  readonly group: number;
}

/** What a group has read so far. */
interface GroupState {
  /** Whether a SELECT came before. */
  select: boolean;
  /** Whether no word came yet. */
  first: boolean;
  /** The word before, in lower case. */
  previous: string;
  /** Whether a FROM clause is open. */
  from: boolean;
  /** What comes next in it. */
  expect: "table" | "alias" | "aliasAfterAs" | "none";
  /** The table an alias would name. */
  last: Found | null;
  /** Whether the group open inside is a derived table, which an alias may follow. */
  derived: boolean;
}

/** What a reading of a text found. */
interface Reading {
  /** The tables. */
  readonly tables: readonly Found[];
  /** Where each group starts (its parenthesis) and ends (its closing one, or the text's end). */
  readonly spans: readonly (readonly [number, number])[];
  /** What the text's innermost open group expects next. */
  readonly place: Place;
}

/**
 * The statement the position is in.
 *
 * @param doc - The document.
 * @param pos - The position.
 * @returns The statement and the position in it.
 */
export function statementAt(doc: Text, pos: number): Statement {
  const text = doc.toString();
  const start = text.lastIndexOf(";", pos - 1) + 1;
  const end = text.indexOf(";", pos);
  return { text: text.slice(start, end < 0 ? text.length : end), at: pos - start };
}

/**
 * Whether a FROM in a group starts a clause.
 *
 * @param state - The group.
 * @returns True for a clause's FROM.
 */
function startsClause(state: GroupState): boolean {
  return (
    state.previous !== "distinct" && (state.select || state.first || state.previous === "delete")
  );
}

/**
 * Reads a token inside an open FROM clause.
 *
 * @param state - The group.
 * @param token - The token.
 * @param lower - The token in lower case.
 * @param group - The group's number.
 * @param tables - Where a table found goes.
 * @returns True when the token was the clause's; false when the clause ended at it.
 */
function inFrom(
  state: GroupState,
  token: string,
  lower: string,
  group: number,
  tables: Found[],
): boolean {
  if (state.expect === "table" && lower !== "lateral") {
    const dotted = DOTTED.test(token);
    state.expect = dotted || SINGLE_WORD.test(token) ? "alias" : "none";
    state.last = null;
    if (dotted) {
      const parts = token.split(".");
      const table = parts.pop() as string;
      state.last = { read: { at: { namespace: parts.join("."), table }, alias: null }, group };
      tables.push(state.last);
    }
    return true;
  }
  if (state.expect === "alias" && lower === "as") {
    state.expect = "aliasAfterAs";
    return true;
  }
  if (state.expect !== "none" && SINGLE_WORD.test(token) && !NOT_ALIAS.has(lower)) {
    if (state.last !== null) {
      state.last.read.alias = token;
    }
    state.expect = "none";
    return true;
  }
  if (lower === "," || lower === "join") {
    state.expect = "table";
    return true;
  }
  if (FROM_ENDS.has(lower)) {
    state.from = false;
    state.expect = "none";
    return false;
  }
  if (lower !== "lateral") {
    state.expect = "none";
  }
  return true;
}

/**
 * Reads a text's FROM clauses.
 *
 * @param source - The text.
 * @returns The tables, the groups, and what the end of the text expects.
 */
function read(source: string): Reading {
  const text = source.replace(BLANKS, (blank) => " ".repeat(blank.length));
  const tables: Found[] = [];
  const spans: [number, number][] = [[0, text.length]];
  const fresh = (): GroupState => ({
    select: false,
    first: true,
    previous: "",
    from: false,
    expect: "none",
    last: null,
    derived: false,
  });
  const states = [fresh()];
  const open = [0];
  for (const match of text.matchAll(TOKEN)) {
    const token = match[0];
    const group = open[open.length - 1] as number;
    const state = states[group] as GroupState;
    if (token === "(") {
      state.derived = state.from && state.expect === "table";
      state.expect = "none";
      state.first = false;
      open.push(states.length);
      spans.push([match.index, text.length]);
      states.push(fresh());
      continue;
    }
    if (token === ")") {
      if (open.length > 1) {
        (spans[group] as [number, number])[1] = match.index;
        open.pop();
        const outer = states[open[open.length - 1] as number] as GroupState;
        if (outer.derived) {
          outer.derived = false;
          outer.expect = "alias";
          outer.last = null;
        }
      }
      continue;
    }
    const lower = token.toLowerCase();
    if (!state.from || !inFrom(state, token, lower, group, tables)) {
      if (lower === "select") {
        state.select = true;
      } else if (lower === "from" && startsClause(state)) {
        state.from = true;
        state.expect = "table";
      }
    }
    state.first = false;
    state.previous = lower;
  }
  const last = states[open[open.length - 1] as number] as GroupState;
  let place: Place = "other";
  if (last.from && last.expect === "table") {
    place = "table";
  } else if (last.from && last.expect !== "none") {
    place = "alias";
  }
  return { tables, spans, place };
}

/**
 * What the cursor is expected to write after a text: a table (after FROM, JOIN or a comma of a
 * FROM list), a table's alias (right after a table), or something else.
 *
 * @param before - The statement's text before the word at the cursor.
 * @returns The place.
 */
export function placeAfter(before: string): Place {
  return read(before).place;
}

/**
 * The tables in scope where the statement's position is, in their order, each once by what the
 * statement calls it.
 *
 * @param statement - The statement and the position.
 * @returns The tables.
 */
export function tablesRead(statement: Statement): TableRead[] {
  const { tables, spans } = read(statement.text);
  const around = new Set<number>([0]);
  spans.forEach(([start, end], group) => {
    if (start < statement.at && statement.at <= end) {
      around.add(group);
    }
  });
  const found = new Map<string, TableRead>();
  for (const { read: table, group } of tables) {
    // The groups around the position are its own and every one outside it.
    if (!around.has(group)) {
      continue;
    }
    const { at, alias } = table;
    const qualifier = alias ?? `${at.namespace}.${at.table}`;
    if (!found.has(qualifier.toLowerCase())) {
      found.set(qualifier.toLowerCase(), { at, alias, qualifier });
    }
  }
  return [...found.values()];
}
