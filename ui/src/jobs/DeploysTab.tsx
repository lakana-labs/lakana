/**
 * The Deploys tab of the Jobs area: one environment's deploys, each repository's current version
 * marked, with Roll back to this… beside every earlier successful one.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
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
import { deployCommit, DeployRow } from "./DeployRow";
import "./deployStrings";
import { Environments, type SomeEnvironments } from "./Environments";
import { mayRollBackTo } from "./rollbackable";
import { RollbackNote } from "./RollbackNote";
import { useJobsFilter } from "./useJobsFilter";

// The rollback's question is its own chunk, read when Roll back to this… is first pressed.
const RollbackDialog = lazy(() =>
  import("./RollbackDialog").then((m) => ({ default: m.RollbackDialog })),
);

/** A rollback the server accepted, which the note follows. */
interface Rolled {
  /** The rollback's id. */
  readonly id: string;
  /** The repository. */
  readonly repository: string;
  /** The commit, as the list writes it. */
  readonly commit: string;
}

/** The Repository menu's choice of every repository; a repository's choice is `repo:` and its name. */
const ALL_REPOSITORIES = "all";

/** Properties of {@link DeploysOf}. */
interface DeploysOfProps {
  /** The environment whose deploys are listed. */
  readonly environment: string;
  /** Whether it is protected. */
  readonly protectedEnvironment: boolean;
}

/**
 * One environment's deploys: the Repository menu (every repository, then those deployed there,
 * in the address as `?repository=`), the line that says why its current versions could not be
 * read, and the deploys, 50 a page with Show more. They are not read again on their own: a
 * deploy is the person's own act, and the list and the current versions are read again once a
 * rollback the person asked for ended. Roll back to this… opens the rollback's question; once
 * the server accepted the rollback, the focus is back on the row's button and the note under the
 * list follows it. A row that leaves the list with the focus in it hands the focus to the note.
 *
 * @param props - The environment and its protection.
 * @param props.environment - The environment.
 * @param props.protectedEnvironment - Whether it is protected.
 * @returns The menu and the list.
 */
function DeploysOf({ environment, protectedEnvironment }: DeploysOfProps): JSX.Element {
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
  const [rolling, setRolling] = useState<DeployOut | null>(null);
  const [rolled, setRolled] = useState<Rolled | null>(null);
  const { reload: reloadPages } = pages;
  const { reload: reloadDeployments } = deployments;
  const onEnded = useCallback(() => {
    reloadPages();
    reloadDeployments();
  }, [reloadPages, reloadDeployments]);
  const area = useRef<HTMLDivElement>(null);
  const note = useRef<HTMLDivElement>(null);
  const inside = useRef(false);
  useEffect(() => {
    const element = area.current;
    const onFocusIn = (): void => {
      inside.current = true;
    };
    const onFocusOut = (event: FocusEvent): void => {
      inside.current = element?.contains(event.relatedTarget as Node | null) === true;
    };
    element?.addEventListener("focusin", onFocusIn);
    element?.addEventListener("focusout", onFocusOut);
    return () => {
      element?.removeEventListener("focusin", onFocusIn);
      element?.removeEventListener("focusout", onFocusOut);
    };
  }, []);
  // Only once the rows change: a dialog that closes hands the focus back to its button itself.
  useEffect(() => {
    if (inside.current && document.activeElement === document.body) {
      note.current?.focus();
    }
  }, [pages.items]);
  const rows = pages.items ?? [];
  return (
    <div ref={area}>
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
      {/* The status region stays in place, so the note is announced when it arrives. */}
      <div className="jobs-rollback-note" role="status" tabIndex={-1} ref={note}>
        {rolled !== null && (
          <RollbackNote
            key={rolled.id}
            environment={environment}
            repository={rolled.repository}
            commit={rolled.commit}
            deployId={rolled.id}
            onEnded={onEnded}
          />
        )}
      </div>
      {rolling !== null && (
        <Suspense fallback={null}>
          <RollbackDialog
            environment={environment}
            deploy={rolling}
            protectedEnvironment={protectedEnvironment}
            onAccepted={(id) => {
              setRolled({
                id,
                repository: rolling.repository,
                commit: deployCommit(rolling) ?? "",
              });
              setRolling(null);
            }}
            onCancel={() => {
              setRolling(null);
            }}
          />
        </Suspense>
      )}
    </div>
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
      <DeploysOf
        key={chosen.name}
        environment={chosen.name}
        protectedEnvironment={chosen.protected}
      />
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
