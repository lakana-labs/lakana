/**
 * Settings' Groups place, loaded on demand: the list of groups, and each group's page below it,
 * which loads on demand too.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import { Route, Routes, useParams } from "react-router";

import { NotFound } from "../shell/NotFound";
import { GroupsPage } from "./GroupsPage";

const GroupPage = lazy(() =>
  import("./GroupPage").then((module) => ({ default: module.GroupPage })),
);

/**
 * A group's page, new for each group, so nothing read for one shows for another.
 *
 * @returns The page.
 */
function GroupRoute(): JSX.Element {
  const { t } = useTranslation();
  const name = useParams()["name"] as string;
  return (
    <Suspense fallback={<p className="step-status">{t("session.loading")}</p>}>
      <GroupPage key={name} name={name} />
    </Suspense>
  );
}

/**
 * Renders the place the address names under `/settings/access/groups`: the list, or a group.
 *
 * @returns The place.
 */
export function GroupsRoutes(): JSX.Element {
  return (
    <Routes>
      <Route index element={<GroupsPage />} />
      <Route path=":name" element={<GroupRoute />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
