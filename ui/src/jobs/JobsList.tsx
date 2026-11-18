/**
 * The Jobs area's own page: what runs now, what failed and why, and every job, across the
 * environments the person may read.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { listEnvironments } from "../api/environments";
import { ReadFailed } from "../catalog/ReadFailed";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { useResource } from "../shell/useResource";
import "./jobStrings";
import { JobsSearch } from "./JobsSearch";
import { Reading } from "./Reading";
import { useJobsFilter } from "./useJobsFilter";

/** The command that lists an environment's jobs. */
const CLI_JOBS = "lakana job list --env <environment>";

/**
 * The list under the heading of `/jobs`: the environment filter and the search, then Running
 * now, Failed, needs attention and All jobs. With no environment to read, it says how to get
 * access.
 *
 * @returns The list.
 */
export function JobsList(): JSX.Element {
  const { t } = useTranslation();
  const environments = useResource(listEnvironments);
  const filter = useJobsFilter(environments.data);
  const listed = environments.data;
  let content: JSX.Element;
  if (listed === undefined) {
    content =
      environments.error === null ? (
        <Reading label={t("jobs.list.loading")} />
      ) : (
        <ReadFailed message={environments.error} onRetry={environments.reload} />
      );
  } else if (listed.length === 0) {
    content = (
      <div className="jobs-no-access">
        <p>{t("jobs.list.noEnvironment")}</p>
        <p>{t("jobs.list.noEnvironmentCli")}</p>
        <CopyLine text={CLI_JOBS} copyLabel={t("jobs.list.copy")} />
      </div>
    );
  } else {
    content = (
      <>
        <div className="jobs-find">
          <EnvironmentPicker
            environments={listed}
            value={filter.environment}
            label={t("jobs.list.environment")}
            all={t("jobs.list.allEnvironments")}
            onChange={filter.setEnvironment}
          />
          <JobsSearch query={filter.query} onSearch={filter.setQuery} />
        </div>
        <div className="jobs-panels">
          <section className="flat-panel">
            <h2 className="jobs-panel-heading">{t("jobs.list.running.heading", { count: 0 })}</h2>
          </section>
          <section className="flat-panel">
            <h2 className="jobs-panel-heading">{t("jobs.list.failed.heading", { count: 0 })}</h2>
          </section>
          <section className="flat-panel">
            <h2 className="jobs-panel-heading">{t("jobs.list.all.heading")}</h2>
          </section>
        </div>
      </>
    );
  }
  return content;
}
