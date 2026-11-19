/**
 * Settings → Environments and the pages below it: the list and each environment's page.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import { Route, Routes, useParams } from "react-router";

import { NotFound } from "../shell/NotFound";
import { EnvironmentsPlace } from "./EnvironmentsPlace";
import { FocusIfLost } from "./FocusIfLost";

// An environment's page loads on its own, so Environments stays small.
const EnvironmentPage = lazy(async () => ({
  default: (await import("./EnvironmentPage")).EnvironmentPage,
}));

/**
 * An environment's page, named by the address; a page of another name starts afresh.
 *
 * @returns The page.
 */
function EnvironmentRoute(): JSX.Element {
  const { t } = useTranslation();
  const name = useParams()["name"] as string;
  return (
    <Suspense fallback={<p className="step-status">{t("session.loading")}</p>}>
      <EnvironmentPage key={name} name={name} />
      <FocusIfLost />
    </Suspense>
  );
}

/**
 * Renders the address below `/settings/environments`: the list at its root and an environment's
 * page at its name.
 *
 * @returns The page.
 */
export function EnvironmentsRoutes(): JSX.Element {
  return (
    <Routes>
      <Route index element={<EnvironmentsPlace />} />
      <Route path=":name" element={<EnvironmentRoute />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
