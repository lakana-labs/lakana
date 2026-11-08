/**
 * The Settings area's screen, loaded on demand; Sign-in is its first tab for now.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Navigate, Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { SignInSettings } from "./SignInSettings";

/**
 * Renders the place the address names under `/settings`; `/settings` itself opens Sign-in.
 *
 * @returns The place.
 */
export function AreaScreen(): JSX.Element {
  return (
    <Routes>
      <Route index element={<Navigate to="sign-in" replace />} />
      <Route path="sign-in" element={<SignInSettings />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
