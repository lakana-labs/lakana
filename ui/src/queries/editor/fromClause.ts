/**
 * The tables a SQL statement reads, as the editor's help needs them: after FROM, JOIN, the commas
 * of a FROM list, UPDATE and DELETE … USING, each with its alias and the parentheses it sits in.
 *
 * @remarks
 * Not a parser of SQL, but enough of one for completion and hover. Comments, strings and quoted
 * identifiers are read as blanks, and a parenthesis that is never closed (a call being typed) is
 * read as no group. A FROM counts as a clause only where it can start one: in a group (the
 * statement, or the inside of a parenthesis) that has a SELECT or an UPDATE before it, as the
 * group's first word (DuckDB's FROM-first) or after DELETE; so `extract(year FROM ts)` and
 * `IS DISTINCT FROM` read no table.
 *
 * Where the cursor is, a table counts when its FROM sits in the cursor's group or one around it,
 * and in the same branch of a UNION, INTERSECT or EXCEPT as the cursor. A CTE's body and a
 * subquery in FROM see nothing outside their parentheses, unless the subquery is LATERAL; a
 * subquery elsewhere (WHERE, SELECT) sees the tables around it. A join in parentheses counts as
 * part of the FROM it is in.
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
  "as",
  "asof",
  "at",
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
  "set",
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
  "set",
  "union",
  "unpivot",
  "where",
  "window",
]);
// Set operators: each starts a branch with its own tables.
const SET_OPERATORS = new Set(["union", "intersect", "except"]);
// The first words of a subquery in parentheses; any other word starts a join in parentheses.
const SUBQUERY_STARTS = new Set(["select", "from", "with", "values", "(", "table"]);

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

/** A table found, with the group and the branch its FROM is in. */
interface Found {
  /** The table, its alias set once read. */
  read: { at: TableAt; alias: string | null };
  /** The group. */
  readonly group: number;
  /** The branch of the group's set operators. */
  readonly branch: number;
}

/** What a group has read so far. */
interface GroupState {
  /** Where it starts (its parenthesis) and ends (its closing one, or the text's end). */
  readonly span: [number, number];
  /** The group around it; -1 for the statement. */
  readonly parent: number;
  /** Whether it sees nothing outside it: a CTE's body, or a subquery in FROM but LATERAL. */
  closed: boolean;
  /** Where its tables go: its own number, or the FROM's around it for a join in parentheses. */
  owner: number;
  /** Whether its first word decides between a subquery and a join in parentheses. */
  pendingKind: boolean;
  /** Where its set operators are. */
  readonly operators: number[];
  /** Whether a SELECT, an UPDATE, a DELETE or a WITH came before. */
  select: boolean;
  /** Whether an UPDATE came before. */
  update: boolean;
  /** Whether a DELETE came before. */
  delete: boolean;
  /** Whether a WITH came before. */
  with: boolean;
  /** Whether a JOIN came in the FROM clause. */
  joined: boolean;
  /** Whether LATERAL came right before. */
  lateral: boolean;
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
  /** The groups; the statement is group 0. */
  readonly groups: readonly GroupState[];
  /** What the innermost group open at the text's end expects next. */
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
  return state.previous !== "distinct" && (state.select || state.first || state.delete);
}

/**
 * Reads a token inside an open FROM clause.
 *
 * @param state - The group.
 * @param token - The token.
 * @param lower - The token in lower case.
 * @param add - Records a table found, and hands it back.
 * @returns True when the token was the clause's; false when the clause ended at it.
 */
function inFrom(
  state: GroupState,
  token: string,
  lower: string,
  add: (at: TableAt) => Found,
): boolean {
  if (state.expect === "table" && lower === "lateral") {
    state.lateral = true;
    return true;
  }
  if (state.expect === "table") {
    const dotted = DOTTED.test(token);
    state.expect = dotted || SINGLE_WORD.test(token) ? "alias" : "none";
    state.last = null;
    if (dotted) {
      const parts = token.split(".");
      const table = parts.pop() as string;
      state.last = add({ namespace: parts.join("."), table });
    }
    return true;
  }
  if (state.expect === "alias" && lower === "as") {
    state.expect = "aliasAfterAs";
    return true;
  }
  // After AS any word is the alias; without it, a keyword is not.
  if (
    state.expect !== "none" &&
    SINGLE_WORD.test(token) &&
    (state.expect === "aliasAfterAs" || !NOT_ALIAS.has(lower))
  ) {
    if (state.last !== null) {
      state.last.read.alias = token;
    }
    state.expect = "none";
    return true;
  }
  if (lower === "," || lower === "join" || (lower === "using" && state.delete && !state.joined)) {
    state.joined ||= lower === "join";
    state.expect = "table";
    return true;
  }
  if (FROM_ENDS.has(lower)) {
    state.from = false;
    state.expect = "none";
    return false;
  }
  state.expect = "none";
  return true;
}

/**
 * The parentheses that open and close a group.
 *
 * @remarks
 * Before the cursor, a closing parenthesis closes the innermost one open. The ones still open
 * at the cursor are the groups the cursor is in, the innermost of them perhaps calls being typed
 * whose closing parenthesis is not written yet. So a closing parenthesis after the cursor that
 * no opening one after the cursor takes closes the outermost of them still open, and those left
 * over, the innermost, are no group: `(select max(a.| from gold.a a)` closes the subquery, not
 * `max(`.
 * @param tokens - The tokens and where they are.
 * @param at - Where the cursor is; null for a text that ends at the cursor, whose parentheses
 *   still open are the groups it is in.
 * @returns The opening parentheses that open a group, and the closing ones that close one.
 */
function matched(
  tokens: readonly { readonly 0: string; readonly index: number }[],
  at: number | null,
): { opens: Set<number>; closes: Set<number> } {
  const opens = new Set<number>();
  const closes = new Set<number>();
  const waiting: number[] = [];
  const after: number[] = [];
  let around: number[] | null = null;
  tokens.forEach((token, index) => {
    if (at !== null && token.index >= at) {
      around ??= [...waiting];
      if (token[0] === "(") {
        after.push(index);
      } else if (token[0] === ")") {
        const opening = after.length > 0 ? after.pop() : around.shift();
        if (opening !== undefined) {
          opens.add(opening);
          closes.add(index);
        }
      }
    } else if (token[0] === "(") {
      waiting.push(index);
    } else if (token[0] === ")" && waiting.length > 0) {
      opens.add(waiting.pop() as number);
      closes.add(index);
    }
  });
  if (at === null) {
    for (const index of waiting) {
      opens.add(index);
    }
  }
  return { opens, closes };
}

/**
 * A new group's state.
 *
 * @param span - Where it starts and ends.
 * @param parent - The group around it.
 * @param owner - Where its tables go.
 * @returns The state.
 */
function group(span: [number, number], parent: number, owner: number): GroupState {
  return {
    span,
    parent,
    closed: false,
    owner,
    pendingKind: false,
    operators: [],
    select: false,
    update: false,
    delete: false,
    with: false,
    joined: false,
    lateral: false,
    first: true,
    previous: "",
    from: false,
    expect: "none",
    last: null,
    derived: false,
  };
}

/**
 * Reads a word outside an open FROM clause: what starts a clause or a branch.
 *
 * @param state - The group.
 * @param lower - The word in lower case.
 * @param index - Where it is.
 */
function outsideFrom(state: GroupState, lower: string, index: number): void {
  if (lower === "select" || lower === "with") {
    state.select = true;
    state.with ||= lower === "with";
  } else if (lower === "update" && state.first) {
    state.select = true;
    state.update = true;
    state.from = true;
    state.expect = "table";
  } else if (lower === "delete" && state.first) {
    state.delete = true;
  } else if (lower === "from" && startsClause(state)) {
    state.from = true;
    state.expect = "table";
  } else if (SET_OPERATORS.has(lower) && state.previous !== "*") {
    state.operators.push(index);
  }
}

/**
 * Reads a text's FROM clauses.
 *
 * @param source - The text.
 * @param at - Where the cursor is in it (see {@link matched}); null for a text that ends at the
 *   cursor.
 * @returns The tables, the groups, and what the end of the text expects.
 */
function read(source: string, at: number | null): Reading {
  const text = source.replace(BLANKS, (blank) => " ".repeat(blank.length));
  const matches = [...text.matchAll(TOKEN)];
  const { opens, closes } = matched(matches, at);
  const tables: Found[] = [];
  const groups = [group([0, text.length], -1, 0)];
  const open = [0];
  matches.forEach((match, index) => {
    const token = match[0];
    const number = open[open.length - 1] as number;
    const state = groups[number] as GroupState;
    if (opens.has(index)) {
      const derived = state.from && state.expect === "table";
      const inner = group([match.index, text.length], number, groups.length);
      // A CTE's body, or a subquery in FROM but LATERAL, once its first word says it is one.
      inner.closed = (state.with && state.previous === "as") || (derived && !state.lateral);
      inner.pendingKind = derived;
      state.derived = derived;
      state.expect = "none";
      state.lateral = false;
      state.first = false;
      open.push(groups.length);
      groups.push(inner);
      return;
    }
    if (closes.has(index)) {
      state.span[1] = match.index;
      open.pop();
      const outer = groups[open[open.length - 1] as number] as GroupState;
      if (outer.derived) {
        outer.derived = false;
        outer.expect = "alias";
        outer.last = null;
      }
      return;
    }
    const lower = token.toLowerCase();
    if (state.pendingKind) {
      state.pendingKind = false;
      if (!SUBQUERY_STARTS.has(lower)) {
        // A join in parentheses: its tables are the FROM's around it.
        state.closed = false;
        state.owner = (groups[state.parent] as GroupState).owner;
        state.from = true;
        state.expect = "table";
      }
    }
    const owner = groups[state.owner] as GroupState;
    const add = (at: TableAt): Found => {
      const found = {
        read: { at, alias: null },
        group: state.owner,
        branch: owner.operators.length,
      };
      tables.push(found);
      return found;
    };
    if (!state.from || !inFrom(state, token, lower, add)) {
      outsideFrom(state, lower, match.index);
    }
    state.first = false;
    state.previous = lower;
  });
  const last = groups[open[open.length - 1] as number] as GroupState;
  let place: Place = "other";
  if (last.from && last.expect === "table") {
    place = "table";
  } else if (last.from && last.expect !== "none") {
    place = "alias";
  }
  return { tables, groups, place };
}

/**
 * What the cursor is expected to write after a text: a table (after FROM, JOIN or a comma of a
 * FROM list), a table's alias (right after a table), or something else.
 *
 * @param before - The statement's text before the word at the cursor.
 * @returns The place.
 */
export function placeAfter(before: string): Place {
  return read(before, null).place;
}

/**
 * The tables in scope where the statement's position is, in their order, each once by what the
 * statement calls it.
 *
 * @param statement - The statement and the position.
 * @returns The tables.
 */
export function tablesRead(statement: Statement): TableRead[] {
  const { tables, groups } = read(statement.text, statement.at);
  const { at } = statement;
  // The innermost group around the position, then outward until a group that sees nothing more.
  let inner = 0;
  groups.forEach(({ span: [start, end] }, number) => {
    if (start < at && at <= end && start >= (groups[inner] as GroupState).span[0]) {
      inner = number;
    }
  });
  const branches = new Map<number, number>();
  for (let number = inner; number >= 0;) {
    const state = groups[number] as GroupState;
    branches.set(number, state.operators.filter((operator) => operator < at).length);
    number = state.closed ? -1 : state.parent;
  }
  const found = new Map<string, TableRead>();
  for (const { read: table, group: number, branch } of tables) {
    if (branches.get(number) !== branch) {
      continue;
    }
    const { at: place, alias } = table;
    const qualifier = alias ?? `${place.namespace}.${place.table}`;
    if (!found.has(qualifier.toLowerCase())) {
      found.set(qualifier.toLowerCase(), { at: place, alias, qualifier });
    }
  }
  return [...found.values()];
}
