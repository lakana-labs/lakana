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
 * (where Home, Recent and Run lead), and the not-found state for every other address.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<QueryWork mode="new" />} />
      <Route path=":env/runs/:id" element={<QueryWork mode="run" />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
