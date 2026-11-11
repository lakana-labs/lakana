/**
 * The Settings area's screen, loaded on demand: Status (a placeholder until part 8) and Sign-in.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Navigate, Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { SignInSettings } from "./SignInSettings";
import { StatusPlaceholder } from "./StatusPlaceholder";

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
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
