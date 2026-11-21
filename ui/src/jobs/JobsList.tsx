/**
 * The Jobs area's own page: what runs now, what failed and why, and every job, across the
 * environments the person may read.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { useTranslation } from "react-i18next";

import type { EnvironmentChoice } from "../api/environments";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { ListToolbar } from "../shell/ListToolbar";
import { AllJobs } from "./AllJobs";
import { Environments } from "./Environments";
import { FailedRuns } from "./FailedRuns";
import "./jobStrings";
import "./listStrings";
import { JobsSearch } from "./JobsSearch";
import { RunningNow } from "./RunningNow";
import { useJobsFilter } from "./useJobsFilter";

/** Properties of {@link JobsSections}. */
interface JobsSectionsProps {
  /** The environments the person may read; at least one. */
  readonly listed: readonly EnvironmentChoice[];
}

/**
 * The environment filter and the search, then Running now, Failed, needs attention and All jobs.
 *
 * @param props - The environments.
 * @param props.listed - The environments the person may read.
 * @returns The filter and the sections.
 */
function JobsSections({ listed }: JobsSectionsProps): JSX.Element {
  const { t } = useTranslation();
  const filter = useJobsFilter(listed);
  // A control that acted reads every section again at once: a run started or resumed shows
  // under Running now, a cancelled one leaves it, a paused schedule changes All jobs.
  const [reloadKey, setReloadKey] = useState(0);
  const onChanged = useCallback(() => {
    setReloadKey((previous) => previous + 1);
  }, []);
  return (
    <>
      {/* Each section's heading carries its own count, so the toolbar has none. */}
      <ListToolbar
        count={null}
        search={<JobsSearch query={filter.query} onSearch={filter.setQuery} />}
        filters={
          <EnvironmentPicker
            environments={listed}
            value={filter.environment}
            label={t("jobs.list.environment")}
            all={t("jobs.list.allEnvironments")}
            onChange={filter.setEnvironment}
          />
        }
        onRefresh={onChanged}
      />
      {/* A new filter mounts the sections afresh: no row, count or control of the previous
          filter stays while the new read is open, and a failed first read says so at once. */}
      <div className="panel-stack">
        <RunningNow
          key={`running:${filter.environment ?? ""}`}
          environment={filter.environment}
          query={filter.query}
          reloadKey={reloadKey}
          onChanged={onChanged}
        />
        <FailedRuns
          key={`failed:${filter.environment ?? ""}`}
          environment={filter.environment}
          query={filter.query}
          reloadKey={reloadKey}
          onChanged={onChanged}
        />
        <AllJobs
          key={`all:${filter.environment ?? ""}`}
          environments={
            filter.environment === null
              ? listed.map((environment) => environment.name)
              : [filter.environment]
          }
          query={filter.query}
          reloadKey={reloadKey}
          onChanged={onChanged}
        />
      </div>
    </>
  );
}

/**
 * The list under the heading of `/jobs`: the environment filter and the search, then Running
 * now, Failed, needs attention and All jobs. With no environment to read, it says how to get
 * access.
 *
 * @returns The list.
 */
export function JobsList(): JSX.Element {
  return <Environments>{(listed) => <JobsSections listed={listed} />}</Environments>;
}
