/**
 * The saved query open on the Queries screen: the one its address names, or the one a run on
 * this page was started from.
 *
 * @packageDocumentation
 */
import { useCallback } from "react";
import { useLocation } from "react-router";

import { getSavedQuery, type SavedQueryOut } from "../api/savedQueries";
import { useResource } from "../shell/useResource";
import type { QueryNoteState } from "./QueryNote";

/** What the history entry of a Queries address carries besides the address. */
export interface QueriesState {
  /** The saved query open there: just saved, or the one the run there was started from. */
  readonly saved?: SavedQueryOut | null;
  /** What was just done to a saved query, said at `/queries` after a delete. */
  readonly note?: QueryNoteState;
  /** Whether the focus stays where it is after the move (see AppShell). */
  readonly keepFocus?: boolean;
}

/** What the Queries address names, besides a new query. */
export interface QueriesAddress {
  /** A run (`/queries/{environment}/runs/{id}`) or a saved query (`…/saved/{id}`). */
  readonly kind: "run" | "saved";
  /** The environment. */
  readonly environment: string;
  /** The run's or the saved query's id. */
  readonly id: string;
}

/** The opened saved query and the state of its read. */
export interface OpenedSaved {
  /** The saved query, or null: none is open, or it is not read yet. */
  readonly opened: SavedQueryOut | null;
  /** Why the saved query the address names could not be read; null otherwise. */
  readonly error: string | null;
}

/**
 * Reads the saved query the address names, unless the history entry already carries it (just
 * saved, or saved again). A run's address keeps the saved query it was started from, so Save
 * and the file name of a download still know it; a run opened from Recent has none.
 *
 * @param address - What the address names; null for a new query.
 * @returns The opened saved query and why it could not be read.
 */
export function useOpenedSaved(address: QueriesAddress | null): OpenedSaved {
  const carried = (useLocation().state as QueriesState | null)?.saved ?? null;
  const environment = address?.kind === "saved" ? address.environment : null;
  const id = address?.id;
  const load = useCallback(
    () => (environment === null ? Promise.resolve(null) : getSavedQuery(environment, String(id))),
    [environment, id],
  );
  const read = useResource(load);
  if (address === null) {
    return { opened: null, error: null };
  }
  if (address.kind === "run") {
    return { opened: carried, error: null };
  }
  const fresh = [carried, read.data].find((saved) => saved?.id === address.id) ?? null;
  return { opened: fresh, error: fresh === null ? read.error : null };
}
