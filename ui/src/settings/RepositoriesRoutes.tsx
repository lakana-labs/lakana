/**
 * Settings → Repositories and the pages below it: the place itself and Connect a repository.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense } from "react";
import { Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { RepositoriesPlace } from "./RepositoriesPlace";

// Connect a repository loads on its own, so Repositories stays small.
const ConnectRepository = lazy(async () => ({
  default: (await import("./ConnectRepository")).ConnectRepository,
}));

/**
 * Renders the address below `/settings/repositories`: the place at its root, Connect a
 * repository at `new`.
 *
 * @returns The page.
 */
export function RepositoriesRoutes(): JSX.Element {
  return (
    <Routes>
      <Route index element={<RepositoriesPlace />} />
      <Route
        path="new"
        element={
          <Suspense fallback={null}>
            <ConnectRepository />
          </Suspense>
        }
      />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
