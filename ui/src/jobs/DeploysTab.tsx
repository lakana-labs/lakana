/**
 * The Deploys tab of the Jobs area: one environment's deploys, each repository's current version
 * marked, with Roll back to this… beside every earlier successful one.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { type DeployOut, listDeployments, listDeploys } from "../api/jobs";
import { ReadFailed } from "../catalog/ReadFailed";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { useResource } from "../shell/useResource";
import { markedText } from "../shell/VisibleText";
import { DeployRow } from "./DeployRow";
import "./deployStrings";
import { Environments, type SomeEnvironments } from "./Environments";
import { mayRollBackTo } from "./rollbackable";
import { useJobsFilter } from "./useJobsFilter";

/** The Repository menu's choice of every repository; a repository's choice is `repo:` and its name. */
const ALL_REPOSITORIES = "all";

/** Properties of {@link DeploysOf}. */
interface DeploysOfProps {
  /** The environment whose deploys are listed. */
  readonly environment: string;
}

/**
 * One environment's deploys: the Repository menu (every repository, then those deployed there,
 * in the address as `?repository=`), the line that says why its current versions could not be
 * read, and the deploys, 50 a page with Show more. They are not read again on their own: a
 * deploy is the person's own act.
 *
 * @param props - The environment.
 * @param props.environment - The environment.
 * @returns The menu and the list.
 */
function DeploysOf({ environment }: DeploysOfProps): JSX.Element {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const repository = params.get("repository");
  const deployments = useResource(useCallback(() => listDeployments(environment), [environment]));
  const load = useCallback(
    (cursor: string | null) => listDeploys(environment, repository, cursor),
    [environment, repository],
  );
  const pages = useCursorPages(JSON.stringify([environment, repository]), load);
  const current = new Map(
    (deployments.data ?? []).map((deployment) => [
      deployment.repository,
      deployment.project_version_id,
    ]),
  );
  const repositories = [...current.keys()];
  if (repository !== null && !current.has(repository)) {
    repositories.push(repository);
  }
  const [, setRolling] = useState<DeployOut | null>(null);
  const rows = pages.items ?? [];
  return (
    <>
      <div className="jobs-find">
        <ChoiceMenu
          className="jobs-filter"
          label={t("jobs.deploys.repository")}
          choices={[
            { id: ALL_REPOSITORIES, text: t("jobs.deploys.allRepositories") },
            ...repositories.map((name) => ({ id: `repo:${name}`, text: markedText(name) })),
          ]}
          selected={repository === null ? ALL_REPOSITORIES : `repo:${repository}`}
          onChoose={(id) => {
            setParams(
              (now) => {
                const next = new URLSearchParams(now);
                if (id === ALL_REPOSITORIES) {
                  next.delete("repository");
                } else {
                  next.set("repository", id.slice("repo:".length));
                }
                return next;
              },
              { replace: true },
            );
          }}
        />
      </div>
      {deployments.error !== null && (
        <ReadFailed
          message={t("jobs.deploys.currentFailed", { sentence: deployments.error })}
          onRetry={deployments.reload}
        />
      )}
      <ViewList
        key={JSON.stringify([environment, repository])}
        pages={pages}
        rowCount={rows.length}
        empty={
          repository === null
            ? t("jobs.deploys.empty", { environment })
            : t("jobs.deploys.emptyOf", { environment, repository })
        }
        region={t("jobs.deploys.region", { environment })}
        scroll="jobs-deploys-scroll"
        head={
          <tr>
            <th scope="col">{t("jobs.deploys.columns.result")}</th>
            <th scope="col">{t("jobs.deploys.columns.kind")}</th>
            <th scope="col">{t("jobs.deploys.columns.commit")}</th>
            <th scope="col">{t("jobs.deploys.columns.repository")}</th>
            <th scope="col">{t("jobs.deploys.columns.who")}</th>
            <th scope="col">{t("jobs.deploys.columns.when")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("jobs.deploys.columns.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((deploy) => (
          <DeployRow
            key={deploy.id}
            deploy={deploy}
            current={
              deploy.project_version_id !== null &&
              current.get(deploy.repository) === deploy.project_version_id
            }
            onRollBack={
              mayRollBackTo(deploy, current)
                ? () => {
                    setRolling(deploy);
                  }
                : null
            }
          />
        ))}
      </ViewList>
    </>
  );
}

/** Properties of {@link DeploysList}. */
interface DeploysListProps {
  /** The environments the person may read; at least one. */
  readonly environments: SomeEnvironments;
}

/**
 * The environment (the Jobs list's shared filter, else the first listed; no "All
 * environments" here) with the line that says deploys are listed one environment at a time, and
 * that environment's deploys.
 *
 * @param props - The environments.
 * @param props.environments - The environments the person may read.
 * @returns The picker and the list.
 */
function DeploysList({ environments }: DeploysListProps): JSX.Element {
  const { t } = useTranslation();
  const filter = useJobsFilter(environments);
  const chosen =
    environments.find((environment) => environment.name === filter.environment) ?? environments[0];
  return (
    <>
      <div className="jobs-find">
        <EnvironmentPicker
          environments={environments}
          value={chosen.name}
          label={t("jobs.list.environment")}
          onChange={filter.setEnvironment}
        />
        <p className="jobs-find-note">{t("jobs.deploys.oneAtATime")}</p>
      </div>
      {/* Each environment's list starts afresh: no row or current version of the previous one
          stays while its own are read. */}
      <DeploysOf key={chosen.name} environment={chosen.name} />
    </>
  );
}

/**
 * Renders the Deploys tab: the environments the person may read, then one environment's deploys.
 *
 * @returns The tab.
 */
export function DeploysTab(): JSX.Element {
  return <Environments>{(listed) => <DeploysList environments={listed} />}</Environments>;
}
