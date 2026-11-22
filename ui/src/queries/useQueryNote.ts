/**
 * The note the Queries screen shows once after a new query or a delete.
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";

import type { QueryNoteState } from "./QueryNote";
import type { QueriesState } from "./useOpenedSaved";

/**
 * Whether two notes say the same.
 *
 * @param held - The note shown.
 * @param carried - The note the history entry carries, if any.
 * @returns True for the same note.
 */
function sameNote(held: QueryNoteState, carried: QueryNoteState | undefined): boolean {
  return carried?.id === held.id;
}

/**
 * The note the history entry brought, shown once: the entry is marked seen as soon as the note
 * shows, so a reload or Back to the entry, which find it seen, show nothing.
 * The note stays while the entry is the one it came with.
 *
 * @returns The note to show, or null.
 */
export function useQueryNote(): QueryNoteState | null {
  const location = useLocation();
  const navigate = useNavigate();
  const state = location.state as QueriesState | null;
  const note = state?.note;
  const fresh = note !== undefined && state?.seen !== true;
  // (State adjusted while rendering, as React documents for state that follows a change.)
  const [held, setHeld] = useState<QueryNoteState | null>(null);
  if (fresh && held !== note) {
    setHeld(note);
  } else if (!fresh && held !== null && !sameNote(held, note)) {
    setHeld(null);
  }
  useEffect(() => {
    if (fresh) {
      const { pathname, search, hash } = location;
      void navigate({ pathname, search, hash }, { replace: true, state: { ...state, seen: true } });
    }
  }, [fresh, state, location, navigate]);
  return held;
}
