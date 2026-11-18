/**
 * Settings' Groups place, loaded on demand: the list of groups, and each group's page below it.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
import { GroupsPage } from "./GroupsPage";

/**
 * Renders the place the address names under `/settings/access/groups`.
 *
 * @returns The place.
 */
export function GroupsRoutes(): JSX.Element {
  return (
    <Routes>
      <Route index element={<GroupsPage />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
