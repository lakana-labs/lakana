/**
 * The Catalog area's screen, loaded on demand.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { CatalogList } from "./CatalogList";
import { TableRoute } from "./table/TableRoute";

/**
 * The Catalog's screen, loaded on demand: the list at `/catalog`, a table's page at
 * `/catalog/{environment}/{namespace}/{table}` (and its tabs below), and the not-found state for
 * every other address.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<CatalogList />} />
      <Route path=":env/:namespace/:table/*" element={<TableRoute />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
