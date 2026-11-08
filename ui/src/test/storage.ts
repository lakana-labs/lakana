/**
 * An in-memory `Storage` for tests, so no test shares the browser's local storage.
 *
 * @packageDocumentation
 */

/** A `Storage` that keeps its items in a map. */
export class MemoryStorage implements Storage {
  readonly #items = new Map<string, string>();

  /**
   * The number of items.
   *
   * @returns How many items are stored.
   */
  get length(): number {
    return this.#items.size;
  }

  /** Removes every item. */
  clear(): void {
    this.#items.clear();
  }

  /**
   * Reads one item.
   *
   * @param key - The item's name.
   * @returns Its value, or null when there is none.
   */
  getItem(key: string): string | null {
    return this.#items.get(key) ?? null;
  }

  /**
   * Names the item at a position.
   *
   * @param index - The position.
   * @returns The name, or null past the end.
   */
  key(index: number): string | null {
    return [...this.#items.keys()][index] ?? null;
  }

  /**
   * Removes one item.
   *
   * @param key - The item's name.
   */
  removeItem(key: string): void {
    this.#items.delete(key);
  }

  /**
   * Stores one item.
   *
   * @param key - The item's name.
   * @param value - Its value.
   */
  setItem(key: string, value: string): void {
    this.#items.set(key, value);
  }
}
