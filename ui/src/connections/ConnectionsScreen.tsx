/**
 * The Connections area's screen, loaded on demand: the databases and services a project's assets
 * load from, with the last test of each.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { ConnectionList } from "./ConnectionList";

/**
 * The Connections screen: the list at `/connections`, and the not-found state for every other
 * address until the pages of a connection, Add and Edit arrive.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<ConnectionList />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
