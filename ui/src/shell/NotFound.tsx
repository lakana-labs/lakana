/**
 * The state of a path the interface does not have, shown inside the shell.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Link } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { Page } from "./Page";

/**
 * Says the page does not exist and offers one way back.
 *
 * @returns The page.
 */
export function NotFound(): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page title={t("notFound.title")}>
      <p>{t("notFound.body")}</p>
      <Link className="button" href="/">
        {t("notFound.action")}
      </Link>
    </Page>
  );
}
