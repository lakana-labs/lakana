/**
 * The Queries area's screen, loaded on demand: write SQL, run it in a chosen environment and
 * see its result.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { QueryWork } from "./QueryWork";

/**
 * The Queries screen: a new query at `/queries`, a run at `/queries/{environment}/runs/{id}`
 * (where Home, Recent and Run lead), a saved query at `/queries/{environment}/saved/{id}`
 * (where Saved and Save as new lead), and the not-found state for every other address. One
 * QueryWork serves the three, so the editor's text stays across their moves.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<QueryWork mode="new" />} />
      <Route path=":env/runs/:id" element={<QueryWork mode="run" />} />
      <Route path=":env/saved/:id" element={<QueryWork mode="saved" />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
