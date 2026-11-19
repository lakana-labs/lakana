/**
 * The deleted environments as Settings › Environments lists them.
 *
 * @packageDocumentation
 */
import { type EnvironmentOut, listDeletedEnvironments } from "../api/environments";

/** A deleted environment as read, and whether its files were due when it was read. */
export type DeletedRow = EnvironmentOut & {
  /** Whether `purge_after` had passed: the server refuses a restore then, and the files go. */
  readonly due: boolean;
};

/**
 * Reads the deleted environments and marks those whose files are due.
 *
 * @returns The rows.
 */
export async function readDeleted(): Promise<readonly DeletedRow[]> {
  const rows = await listDeletedEnvironments();
  const now = Date.now();
  // The server answers purge_after for every deleted environment.
  return rows.map((row) => ({ ...row, due: Date.parse(row.purge_after as string) <= now }));
}
