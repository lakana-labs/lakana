/**
 * A run's page: for now its id under its breadcrumb; the run's detail comes in the next changes.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router";

import { Page } from "../shell/Page";
import "./jobStrings";

/**
 * Renders a run's page.
 *
 * @returns The page of the run the address names.
 */
export function RunPage(): JSX.Element {
  const { t } = useTranslation();
  const { env = "", id = "" } = useParams();
  return (
    <Page
      title={t("jobs.runTitle", { id })}
      trail={[
        { to: "/jobs", label: t("areas.jobs.name") },
        { to: "/jobs", label: env },
      ]}
    >
      <></>
    </Page>
  );
}
