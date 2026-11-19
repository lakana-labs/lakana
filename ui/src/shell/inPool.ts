/**
 * Reads a few things at a time, so a long list of environments never floods the server.
 *
 * @packageDocumentation
 */

/** How many reads of one kind a screen has on their way at once. */
export const READS_IN_FLIGHT = 4;

/**
 * Runs `work` over `items`, at most `limit` at once.
 *
 * @param items - The items.
 * @param limit - How many run at once.
 * @param work - What runs for each; it never rejects.
 */
export async function inPool(
  items: readonly string[],
  limit: number,
  work: (item: string) => Promise<void>,
): Promise<void> {
  const queue = [...items];
  const worker = async (): Promise<void> => {
    for (let item = queue.shift(); item !== undefined; item = queue.shift()) {
      await work(item);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, queue.length) }, worker));
}
