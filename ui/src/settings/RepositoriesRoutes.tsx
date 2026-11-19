/**
 * Settings → Repositories and the pages below it: the list, each repository's page, and
 * Connect a repository for those who manage repositories.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useLayoutEffect } from "react";
import { useTranslation } from "react-i18next";
import { NavigationType, Route, Routes, useNavigationType, useParams } from "react-router";

import { NotAvailable } from "../shell/NotAvailable";
import { NotFound } from "../shell/NotFound";
import { mayManageRepositories } from "./places";
import { RepositoriesPlace } from "./RepositoriesPlace";
import { useSettingsViewer } from "./viewer";

// Connect a repository and a repository's page load on their own, so Repositories stays small.
const ConnectRepository = lazy(async () => ({
  default: (await import("./ConnectRepository")).ConnectRepository,
}));

const RepositoryPage = lazy(async () => ({
  default: (await import("./RepositoryPage")).RepositoryPage,
}));

/**
 * Connect a repository for those who manage repositories; for anyone else the sentence that
 * they cannot use it, without loading it.
 *
 * @returns The page.
 */
function ConnectRoute(): JSX.Element {
  const { t } = useTranslation();
  return mayManageRepositories(useSettingsViewer()) === true ? (
    <Suspense fallback={null}>
      <ConnectRepository />
    </Suspense>
  ) : (
    <NotAvailable place={t("settings.repositories.connect")} />
  );
}

/**
 * Gives the page's heading the focus once a page that was loading has shown after a move, when
 * the focus was left on nothing (the link that was pressed is gone with the list). On the first
 * load the skip link stays the first stop.
 *
 * @returns Nothing to show.
 */
function FocusIfLost(): null {
  const moved = useNavigationType() === NavigationType.Push;
  // A layout effect, as the shell's: the focus moves in the commit that shows the page, so
  // no key pressed in between goes to the page's body.
  useLayoutEffect(() => {
    if (moved && document.activeElement === document.body) {
      document.getElementById("main")?.querySelector("h1")?.focus();
    }
  }, [moved]);
  return null;
}

/**
 * A repository's page, named by the address; a page of another name starts afresh.
 *
 * @returns The page.
 */
function RepositoryRoute(): JSX.Element {
  const { t } = useTranslation();
  const name = useParams()["name"] as string;
  return (
    <Suspense fallback={<p className="step-status">{t("session.loading")}</p>}>
      <RepositoryPage key={name} name={name} />
      <FocusIfLost />
    </Suspense>
  );
}

/**
 * Renders the address below `/settings/repositories`: the list at its root, Connect a
 * repository at `new`, and a repository's page at its name (or at `…/about` for a repository
 * named `new`).
 *
 * @returns The page.
 */
export function RepositoriesRoutes(): JSX.Element {
  return (
    <Routes>
      <Route index element={<RepositoriesPlace />} />
      <Route path="new" element={<ConnectRoute />} />
      <Route path=":name" element={<RepositoryRoute />} />
      <Route path=":name/about" element={<RepositoryRoute />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
