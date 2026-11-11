/**
 * The Settings area's screen, loaded on demand: Status (a placeholder until part 8), Sign-in and
 * Connect a repository.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { SignInSettings } from "./SignInSettings";
import { StatusPlaceholder } from "./StatusPlaceholder";

// Connect a repository loads on its own, so Settings' other places stay small.
const ConnectRepository = lazy(async () => ({
  default: (await import("./ConnectRepository")).ConnectRepository,
}));

/**
 * Renders the place the address names under `/settings`; `/settings` itself opens Status, where
 * Home's "Details" goes.
 *
 * @returns The place.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<Navigate to="status" replace />} />
      <Route path="status" element={<StatusPlaceholder />} />
      <Route path="sign-in" element={<SignInSettings />} />
      <Route
        path="repositories/new"
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
