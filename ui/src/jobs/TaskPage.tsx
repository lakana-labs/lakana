/**
 * A task's page: for now its asset under its breadcrumb; the attempts and the log come in the
 * next changes.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router";

import { Page } from "../shell/Page";
import { jobRunPath } from "./paths";
import "./jobStrings";

/**
 * Renders a task's page.
 *
 * @returns The page of the task the address names.
 */
export function TaskPage(): JSX.Element {
  const { t } = useTranslation();
  const { env = "", id = "", asset = "" } = useParams();
  return (
    <Page
      title={asset}
      trail={[
        { to: "/jobs", label: t("areas.jobs.name") },
        { to: "/jobs", label: env },
        { to: jobRunPath(env, id), label: t("jobs.runTitle", { id }) },
      ]}
    >
      <></>
    </Page>
  );
}
