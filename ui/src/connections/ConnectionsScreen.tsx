/**
 * The Connections area's screen, loaded on demand: the databases and services a project's assets
 * load from, with the last test of each.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes, useParams } from "react-router";

import { NotFound } from "../shell/NotFound";
import { ConnectionList } from "./ConnectionList";
import { ConnectionPage } from "./ConnectionPage";

/**
 * Renders the page of the connection the address names, afresh for each connection.
 *
 * @returns The page.
 */
function Connection(): JSX.Element {
  const { environment, name } = useParams() as { environment: string; name: string };
  return <ConnectionPage key={`${environment}/${name}`} environment={environment} name={name} />;
}

/**
 * The Connections screen: the list at `/connections`, a connection's page at
 * `/connections/{environment}/{name}`, and the not-found state for every other address until Add
 * and Edit arrive.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<ConnectionList />} />
      <Route path=":environment/:name" element={<Connection />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
