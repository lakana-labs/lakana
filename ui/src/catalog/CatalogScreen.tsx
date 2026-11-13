/**
 * The Catalog area's screen, loaded on demand.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { CatalogList } from "./CatalogList";

/**
 * The Catalog's screen, loaded on demand: the list at `/catalog`, and the not-found state under it
 * until the table page exists.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<CatalogList />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
