/**
 * The job's page: for now its name under its breadcrumb; the facts, the runs and the controls
 * come in the next changes.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router";

import { Page } from "../shell/Page";
import "./jobStrings";

/**
 * Renders the job's page.
 *
 * @returns The page of the job the address names.
 */
export function JobPage(): JSX.Element {
  const { t } = useTranslation();
  const { env = "", job = "" } = useParams();
  return (
    <Page
      title={job}
      trail={[
        { to: "/jobs", label: t("areas.jobs.name") },
        { to: "/jobs", label: env },
      ]}
    >
      <></>
    </Page>
  );
}
