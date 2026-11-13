/**
 * The place a person last chose in the Catalog, kept in this browser.
 *
 * @packageDocumentation
 */
import { browserStorage } from "../preferences/preferences";

/** A chosen place: an environment, and a namespace inside it. */
export interface Choice {
  /** The environment; null for all tables. */
  readonly environment: string | null;
  /** The dotted namespace; null for none. */
  readonly namespace: string | null;
}

/**
 * The storage key of a person.
 *
 * @param person - The person's name.
 * @returns The key.
 */
function keyOf(person: string): string {
  return `lakana.catalog.${person}`;
}

/**
 * Whether a stored value names a place or none.
 *
 * @param value - The stored value.
 * @returns True for a string or null.
 */
function isPlace(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

/**
 * Reads the person's last choice.
 *
 * @param person - The person's name.
 * @param storage - Where it is kept; null when the browser has none.
 * @returns The choice, or null when there is none or it cannot be read.
 */
export function readChoice(
  person: string,
  storage: Storage | null = browserStorage(),
): Choice | null {
  let stored: unknown = null;
  try {
    stored = JSON.parse(storage?.getItem(keyOf(person)) ?? "null");
  } catch {
    return null;
  }
  if (typeof stored !== "object" || stored === null) {
    return null;
  }
  const { environment, namespace } = stored as Record<string, unknown>;
  if (!isPlace(environment) || !isPlace(namespace)) {
    return null;
  }
  return { environment, namespace: environment === null ? null : namespace };
}

/**
 * Keeps the person's choice; a storage that refuses is ignored.
 *
 * @param person - The person's name.
 * @param choice - The choice.
 * @param storage - Where to keep it; null when the browser has none.
 */
export function writeChoice(
  person: string,
  choice: Choice,
  storage: Storage | null = browserStorage(),
): void {
  try {
    storage?.setItem(keyOf(person), JSON.stringify(choice));
  } catch {
    // A browser that refuses storage just forgets the choice.
  }
}
