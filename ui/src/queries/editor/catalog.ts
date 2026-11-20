/**
 * What the editor knows of the catalog, for completion and hover: namespaces, table names and
 * columns, read through the existing routes and kept for the page's life.
 *
 * @remarks
 * Nothing here asks the server on every keystroke. The tree is read once for all environments,
 * table names in at most {@link TABLE_PAGES} pages, and a table's columns once, each kept for
 * {@link STALE_MS}. Only an environment with more than 1,000 tables is searched by the typed word,
 * after {@link SEARCH_AFTER_CHARS} characters and {@link SEARCH_QUIET_MS} of quiet, and each word
 * once. A read that fails, a refusal (403) included, answers nothing rather than an error: help in
 * the editor never stands in the way of writing SQL. A refusal is kept like an answer, since
 * another read would answer the same; a failure that may pass (a 5xx, a timeout, no answer) is
 * kept for {@link FAILURE_HOLD_MS} only, so a server that is down gets one read per table in that
 * time however often the person types, and the first keystroke after it reads again. Everything
 * kept is what the signed-in person
 * may read, so it is forgotten when the session ends (`onSessionEnd`): the next person to sign in
 * in the same tab reads their own.
 * @packageDocumentation
 */
import { getTableTree, listCompletionTables, type TableTreeOut } from "../../api/catalog";
import { ApiError, onSessionEnd } from "../../api/client";
import { type ColumnOut, getTable } from "../../api/table";

/** How long a read is kept before it is read again, in milliseconds (five minutes). */
export const STALE_MS = 300_000;
/**
 * How long a failure that may pass answers before it is read again, in milliseconds (15 s).
 */
export const FAILURE_HOLD_MS = 15_000;
/** How many pages of 200 table names are read for an environment. */
export const TABLE_PAGES = 5;
/** How many characters of a table's name are typed before the server is searched. */
export const SEARCH_AFTER_CHARS = 2;
/** How long the typing must pause before the server is searched, in milliseconds. */
export const SEARCH_QUIET_MS = 300;
/** How many reads of a table's columns may wait for the server at once. */
export const COLUMN_READS_IN_FLIGHT = 4;

// Above this many tables in an environment, its first pages do not hold them all, and a typed
// word is searched for.
const SEARCH_ABOVE = 1_000;

/** The catalog of one environment, as completion and hover ask it. */
export interface CompletionCatalog {
  /** The environment's namespaces, by their dotted paths; none when they cannot be read. */
  namespaces(): Promise<readonly string[]>;
  /**
   * The names of a namespace's tables that the person may read: from the first pages, or from a
   * search by the typed word in a large environment.
   */
  tables(namespace: string, prefix: string): Promise<readonly string[]>;
  /** A table's columns; null when it cannot be read, for a refusal too. */
  columns(namespace: string, table: string): Promise<readonly ColumnOut[] | null>;
}

/** A read and until when it answers. */
interface Kept<T> {
  /** Until when it answers, in milliseconds: shortened once it has answered a failure that may pass. */
  until: number;
  /** Its answer. */
  readonly value: Promise<T>;
}

/** What a read answered, and whether it may be kept. */
interface Answer<T> {
  /** The answer. */
  readonly value: T;
  /** False after a failure that may pass: then it is kept for {@link FAILURE_HOLD_MS} only. */
  readonly keep: boolean;
}

/** A table's place and name. */
interface Named {
  /** The dotted namespace. */
  readonly namespace: string;
  /** The table's name. */
  readonly name: string;
}

// The tree, read once for every environment.
let tree: Kept<TableTreeOut | null> | undefined;
const catalogs = new Map<string, CompletionCatalog>();

// One more each time a session ends: a catalog built before then, which an editor may still
// hold, hands every question to the next person's catalog instead of answering from its own.
let generation = 0;

onSessionEnd(() => {
  tree = undefined;
  catalogs.clear();
  generation += 1;
});

// The reads of columns that wait for a free place, and how many places are taken.
const waiting: (() => void)[] = [];
let inFlight = 0;

/**
 * Runs a read once fewer than {@link COLUMN_READS_IN_FLIGHT} others are waiting for the server.
 *
 * @param read - The read.
 * @returns Its answer.
 */
async function limited<T>(read: () => Promise<T>): Promise<T> {
  if (inFlight < COLUMN_READS_IN_FLIGHT) {
    inFlight += 1;
  } else {
    // The read that ends hands its place on, so the count stays as it is.
    await new Promise<void>((resolve) => waiting.push(resolve));
  }
  try {
    return await read();
  } finally {
    const next = waiting.shift();
    if (next === undefined) {
      inFlight -= 1;
    } else {
      next();
    }
  }
}

/**
 * Whether a failure is a refusal, which another read would answer the same: a 4xx, but for a
 * timeout (408) and too many requests (429). A 5xx, no answer and an error of the page's own may
 * pass.
 *
 * @param failure - What the read threw.
 * @returns True for a refusal.
 */
function isRefusal(failure: unknown): boolean {
  return (
    failure instanceof ApiError &&
    failure.status >= 400 &&
    failure.status < 500 &&
    failure.status !== 408 &&
    failure.status !== 429
  );
}

/**
 * A read's answer, or `fallback` after a failure, kept only when the failure was a refusal.
 *
 * @param read - The read.
 * @param fallback - What a failure answers.
 * @returns The answer.
 */
function answered<T, F>(read: Promise<T>, fallback: F): Promise<Answer<T | F>> {
  return read.then(
    (value) => ({ value, keep: true }),
    (failure: unknown) => ({ value: fallback, keep: isRefusal(failure) }),
  );
}

/**
 * Whether a kept read may still answer.
 *
 * @param kept - The kept read, if any.
 * @param now - The clock.
 * @returns True when it may answer.
 */
function isFresh<T>(kept: Kept<T> | undefined, now: () => number): kept is Kept<T> {
  return kept !== undefined && now() < kept.until;
}

/**
 * A kept read while it may answer, else a new one, kept for {@link STALE_MS}, or for
 * {@link FAILURE_HOLD_MS} from its answer when that is a failure that may pass.
 *
 * @param kept - The kept read, if any.
 * @param now - The clock.
 * @param read - Asks again.
 * @returns The read to use, which the caller keeps.
 */
function fresh<T>(
  kept: Kept<T> | undefined,
  now: () => number,
  read: () => Promise<Answer<T>>,
): Kept<T> {
  if (isFresh(kept, now)) {
    return kept;
  }
  const made: Kept<T> = {
    until: now() + STALE_MS,
    value: read().then(({ value, keep }) => {
      if (!keep) {
        made.until = now() + FAILURE_HOLD_MS;
      }
      return value;
    }),
  };
  return made;
}

/**
 * The first pages of an environment's tables, by name, optionally only those matching a word.
 *
 * @param environment - The environment.
 * @param q - The word; empty for every table.
 * @returns The tables read before the last page, the fifth or a failure; kept unless a failure
 *   may pass.
 */
async function readTables(environment: string, q: string): Promise<Answer<readonly Named[]>> {
  const found: Named[] = [];
  let cursor: string | null = null;
  try {
    for (let page = 0; page < TABLE_PAGES; page += 1) {
      const answer = await listCompletionTables(environment, q, cursor);
      found.push(...answer.items);
      cursor = answer.next_cursor;
      if (cursor === null) {
        break;
      }
    }
  } catch (failure: unknown) {
    // What was read answers; the rest is missing until the read is read again.
    return { value: found, keep: isRefusal(failure) };
  }
  return { value: found, keep: true };
}

/**
 * Builds the catalog of one environment; after the session ends it answers from the next one's.
 *
 * @param environment - The environment.
 * @param now - The clock.
 * @returns The catalog.
 */
function build(environment: string, now: () => number): CompletionCatalog {
  const era = generation;
  const own = buildOwn(environment, now);
  const live = (): CompletionCatalog => (era === generation ? own : catalogCache(environment, now));
  return {
    namespaces: () => live().namespaces(),
    tables: (namespace, prefix) => live().tables(namespace, prefix),
    columns: (namespace, table) => live().columns(namespace, table),
  };
}

/**
 * Builds the catalog of one environment for the session it is built in.
 *
 * @param environment - The environment.
 * @param now - The clock.
 * @returns The catalog.
 */
function buildOwn(environment: string, now: () => number): CompletionCatalog {
  let first: Kept<readonly Named[]> | undefined;
  const searches = new Map<string, Kept<readonly Named[]>>();
  const columns = new Map<string, Kept<readonly ColumnOut[] | null>>();
  // The latest word asked for, so that a word typed over within the quiet time is not searched.
  let latest = 0;

  const ownTree = async (): Promise<TableTreeOut["environments"][number] | undefined> => {
    tree = fresh(tree, now, () => answered(getTableTree(), null));
    const read = await tree.value;
    return read?.environments.find((each) => each.name === environment);
  };
  const firstPages = (): Promise<readonly Named[]> => {
    first = fresh(first, now, () => readTables(environment, ""));
    return first.value;
  };
  const search = async (prefix: string): Promise<readonly Named[]> => {
    if (!isFresh(searches.get(prefix), now)) {
      latest += 1;
      const mine = latest;
      await new Promise((resolve) => setTimeout(resolve, SEARCH_QUIET_MS));
      if (mine !== latest) {
        return firstPages();
      }
    }
    const read = fresh(searches.get(prefix), now, () => readTables(environment, prefix));
    searches.set(prefix, read);
    return read.value;
  };

  return {
    namespaces: async () => (await ownTree())?.namespaces.map((each) => each.path) ?? [],
    tables: async (namespace, prefix) => {
      const large = ((await ownTree())?.count ?? 0) > SEARCH_ABOVE;
      const tables =
        large && prefix.length >= SEARCH_AFTER_CHARS ? await search(prefix) : await firstPages();
      return tables.filter((each) => each.namespace === namespace).map((each) => each.name);
    },
    columns: (namespace, table) => {
      const key = JSON.stringify([namespace, table]);
      const read = fresh(columns.get(key), now, () =>
        answered(
          limited(() => getTable({ environment, namespace, name: table })).then(
            (found) => found.schema,
          ),
          null,
        ),
      );
      columns.set(key, read);
      return read.value;
    },
  };
}

/**
 * The catalog of an environment, the same one for the page's life.
 *
 * @param environment - The environment.
 * @param now - The clock, in milliseconds; `Date.now` by default.
 * @returns The catalog.
 */
export function catalogCache(environment: string, now: () => number = Date.now): CompletionCatalog {
  let catalog = catalogs.get(environment);
  if (catalog === undefined) {
    catalog = build(environment, now);
    catalogs.set(environment, catalog);
  }
  return catalog;
}
