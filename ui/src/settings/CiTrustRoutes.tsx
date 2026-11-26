/**
 * Settings › CI trust and the pages below it: the list at its root, Explain a CI job's claims
 * at `explain`, and a rule's page at its id.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import { Route, Routes, useParams } from "react-router";

import { NotFound } from "../shell/NotFound";
import { CiTrustPlace } from "./CiTrustPlace";
import { FocusIfLost } from "./FocusIfLost";

// A rule's page and Explain load on their own, so the list stays small.
const CiTrustRulePage = lazy(async () => ({
  default: (await import("./CiTrustRulePage")).CiTrustRulePage,
}));

const CiTrustExplain = lazy(async () => ({
  default: (await import("./CiTrustExplain")).CiTrustExplain,
}));

/**
 * A rule's page, named by the address; a page of another rule starts afresh.
 *
 * @returns The page.
 */
function RuleRoute(): JSX.Element {
  const { t } = useTranslation();
  const id = useParams()["id"] as string;
  return (
    <Suspense fallback={<p className="step-status">{t("session.loading")}</p>}>
      <CiTrustRulePage key={id} id={id} />
      <FocusIfLost />
    </Suspense>
  );
}

/**
 * Explain, while it loads.
 *
 * @returns The page.
 */
function ExplainRoute(): JSX.Element {
  const { t } = useTranslation();
  return (
    <Suspense fallback={<p className="step-status">{t("session.loading")}</p>}>
      <CiTrustExplain />
    </Suspense>
  );
}

/**
 * Renders the address below `/settings/ci-trust`. Rule ids are UUIDs, so `explain` names no
 * rule.
 *
 * @returns The page.
 */
export function CiTrustRoutes(): JSX.Element {
  return (
    <Routes>
      <Route index element={<CiTrustPlace />} />
      <Route path="explain" element={<ExplainRoute />} />
      <Route path=":id" element={<RuleRoute />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
