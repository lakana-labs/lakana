/**
 * The Deploys tab of the Jobs area: one environment's deploys, each repository's current version
 * marked, with Roll back to this… beside every earlier successful one and Undeploy… beside the
 * current one.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { type DeploymentOut, type DeployOut, listDeployments, listDeploys } from "../api/jobs";
import { ReadFailed } from "../catalog/ReadFailed";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { countWords, ListToolbar, useSteadyMore } from "../shell/ListToolbar";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import type { ListSortSpec } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { settingParam, useAddressParams } from "../shell/useAddressParams";
import { useListSort } from "../shell/useListSort";
import { useResource } from "../shell/useResource";
import { markedText } from "../shell/VisibleText";
import { DeployRow } from "./DeployRow";
import "./deployStrings";
import { Environments, type SomeEnvironments } from "./Environments";
import { currentDeploys, mayRollBackTo, type RollbackTarget } from "./rollbackable";
import { RollbackNote } from "./RollbackNote";
import { useJobsFilter } from "./useJobsFilter";

// The rollback's question is its own chunk, read when Roll back to this… is first pressed.
const RollbackDialog = lazy(() =>
  import("./RollbackDialog").then((m) => ({ default: m.RollbackDialog })),
);

// The undeploy's question likewise, read when Undeploy… is first pressed.
const UndeployDialog = lazy(() =>
  import("./UndeployDialog").then((m) => ({ default: m.UndeployDialog })),
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

/** The repositories undeployed since the current versions were last read. */
interface Undeployed {
  /** The repositories, in the order they were undeployed. */
  readonly repositories: readonly string[];
  /** The current versions as read before them, so the list knows when they were read again. */
  readonly before: readonly DeploymentOut[] | undefined;
}

/** An undeploy the note says was done; a new object for each, so the note takes the focus. */
interface UndeployNote {
  /** The repository. */
  readonly repository: string;
}

/** The Repository menu's choice of every repository; a repository's choice is `repo:` and its name. */
const ALL_REPOSITORIES = "all";

/** The columns the deploys sort by: the newest first unless the address says otherwise. */
const DEPLOY_SORTS: ListSortSpec<"status" | "created"> = {
  fields: { status: "asc", created: "desc" },
  natural: { field: "created", order: "desc" },
};

/**
 * A deploy's identity, so a deploy a later page repeats (its state changed while the list was
 * paged by state) is shown once.
 *
 * @param deploy - The deploy.
 * @returns Its id.
 */
const deployId = (deploy: DeployOut): string => deploy.id;

/** Properties of {@link DeploysOf}. */
interface DeploysOfProps {
  /** The environment whose deploys are listed. */
  readonly environment: string;
  /** Whether it is protected. */
  readonly protectedEnvironment: boolean;
}

/**
 * One environment's deploys, the newest run of each repository's current version marked
 * current: the Repository menu (every repository, then by name those deployed there and those
 * whose deploys were listed, in the address as `?repository=`), the line that says why its current versions could not be
 * read, and the deploys, 50 a page with Show more. They are not read again on their own: a
 * deploy is the person's own act, and the list and the current versions are read again once a
 * rollback the person asked for ended, or once an undeploy was carried out. Roll back to this…
 * opens the rollback's question; once the server accepted the rollback, the focus is back on the
 * row's button and the note under the list follows it. Undeploy…, beside the current deploy,
 * opens the undeploy's question; once the server undeployed the repository, the note says so and
 * takes the focus, nothing of that repository is offered any more, and every row's button waits
 * (pointing at the note) until the current versions were read again. A row that leaves the list
 * with the focus in it hands the focus to the note.
 *
 * @param props - The environment and its protection.
 * @param props.environment - The environment.
 * @param props.protectedEnvironment - Whether it is protected.
 * @returns The menu and the list.
 */
function DeploysOf({ environment, protectedEnvironment }: DeploysOfProps): JSX.Element {
  const { t } = useTranslation();
  const [params, update] = useAddressParams();
  const repository = params.get("repository");
  const deployments = useResource(useCallback(() => listDeployments(environment), [environment]));
  const sorting = useListSort(DEPLOY_SORTS);
  const sort = sorting.choice;
  const load = useCallback(
    (cursor: string | null) => listDeploys(environment, repository, cursor, sort),
    [environment, repository, sort],
  );
  // A new sort reads the first page again; the rows stay until it arrives.
  const pages = useCursorPages(
    JSON.stringify([environment, repository]),
    load,
    JSON.stringify(sort),
    deployId,
  );
  const [undeploying, setUndeploying] = useState<DeploymentOut | null>(null);
  const [undeployed, setUndeployed] = useState<Undeployed | null>(null);
  const [undeployNote, setUndeployNote] = useState<UndeployNote | null>(null);
  // The current versions from before the undeploys: they still name those repositories, which
  // are gone, until they are read again successfully (a failed read keeps the old ones).
  const stale = undeployed !== null && deployments.data === undeployed.before;
  const gone: readonly string[] = stale ? undeployed.repositories : [];
  const current = new Map(
    (deployments.data ?? [])
      .filter((deployment) => !gone.includes(deployment.repository))
      .map((deployment) => [deployment.repository, deployment.project_version_id]),
  );
  // The repositories of the deploys read so far: the menu keeps offering them while the list
  // shows one repository's deploys. The server lists no repository's deploy history of its own,
  // so a repository whose deploys are all older than the pages read is not offered until then.
  const [seen, setSeen] = useState<readonly string[]>([]);
  const unseen = (pages.items ?? [])
    .map((deploy) => deploy.repository)
    .filter((name) => !seen.includes(name));
  if (unseen.length > 0) {
    setSeen([...seen, ...new Set(unseen)]);
  }
  const repositories = [
    ...new Set([...current.keys(), ...seen, ...(repository === null ? [] : [repository])]),
  ].sort((first, second) => first.localeCompare(second));
  const [rolling, setRolling] = useState<RollbackTarget | null>(null);
  const [rolled, setRolled] = useState<Rolled | null>(null);
  const { reload: reloadPages } = pages;
  const { reload: reloadDeployments } = deployments;
  const onEnded = useCallback(() => {
    reloadPages();
    reloadDeployments();
  }, [reloadPages, reloadDeployments]);
  // The rollback that ended. While the one followed runs, no row starts another: a second one
  // would replace the note that follows the first.
  const [endedId, setEndedId] = useState<string | null>(null);
  const noteId = useId();
  const following = rolled !== null && endedId !== rolled.id;
  // While a rollback is followed or the current versions are read again after an undeploy, no
  // row starts anything: the versions it would act on are not known yet.
  const holding = following || (stale && deployments.busy);
  const area = useRef<HTMLDivElement>(null);
  const note = useRef<HTMLDivElement>(null);
  // The element of the list that last had the focus.
  const last = useRef<Element | null>(null);
  useEffect(() => {
    const element = area.current;
    const onFocusIn = (event: FocusEvent): void => {
      last.current = event.target as Element | null;
    };
    element?.addEventListener("focusin", onFocusIn);
    return () => {
      element?.removeEventListener("focusin", onFocusIn);
    };
  }, []);
  // Once the rows or the current versions change (they answer in either order, and either can
  // take a row's button away): a button that left the list with the focus hands it to the note.
  // A dialog that closes hands the focus back to its button itself.
  useEffect(() => {
    const lost = last.current;
    if (lost !== null && !lost.isConnected && document.activeElement === document.body) {
      note.current?.focus();
    }
  }, [pages.items, deployments.data]);
  // The undeploy's note takes the focus at once: the row's Undeploy… left with the dialog.
  useEffect(() => {
    if (undeployNote !== null) {
      note.current?.focus();
    }
  }, [undeployNote]);
  const rows = pages.items ?? [];
  // Nothing is marked while a first page is on its way: after a new sort the rows of the old one
  // stay until it arrives. Show more keeps the mark (its rows only add older ones in the newest
  // first order). In another order than the newest first, a page still to be read may hold a
  // repository's newest deploy: nothing is marked until every page is read.
  const firstRead = pages.busy && !pages.hasMore;
  const marked =
    !firstRead && (sort === null || !pages.hasMore)
      ? currentDeploys(rows, current)
      : new Set<string>();
  const steadyMore = useSteadyMore(pages.hasMore, pages.busy);
  return (
    <div ref={area}>
      <ListToolbar
        count={countWords(t, "jobs.deploys", pages.items?.length, steadyMore)}
        filters={
          <ChoiceMenu
            className="jobs-filter"
            label={t("jobs.deploys.repository")}
            note={t("jobs.deploys.repositoryNote", { environment })}
            choices={[
              { id: ALL_REPOSITORIES, text: t("jobs.deploys.allRepositories") },
              ...repositories.map((name) => ({ id: `repo:${name}`, text: markedText(name) })),
            ]}
            selected={repository === null ? ALL_REPOSITORIES : `repo:${repository}`}
            onChoose={(id) => {
              update(
                settingParam(
                  "repository",
                  id === ALL_REPOSITORIES ? null : id.slice("repo:".length),
                ),
                { replace: true },
              );
            }}
          />
        }
        onRefresh={() => {
          pages.reload();
          deployments.reload();
        }}
        refreshing={pages.busy}
      />
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
            <SortHeader {...sorting.header("status")}>
              {t("jobs.deploys.columns.result")}
            </SortHeader>
            <th scope="col">{t("jobs.deploys.columns.kind")}</th>
            <th scope="col">{t("jobs.deploys.columns.commit")}</th>
            <th scope="col">{t("jobs.deploys.columns.repository")}</th>
            <th scope="col">{t("jobs.deploys.columns.who")}</th>
            <SortHeader {...sorting.header("created")}>{t("jobs.deploys.columns.when")}</SortHeader>
            <th scope="col">
              <span className="visually-hidden">{t("jobs.deploys.columns.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((deploy) => {
          const deployment = marked.has(deploy.id)
            ? deployments.data?.find((one) => one.repository === deploy.repository)
            : undefined;
          return (
            <DeployRow
              key={deploy.id}
              deploy={deploy}
              current={marked.has(deploy.id)}
              onRollBack={
                mayRollBackTo(deploy, current)
                  ? () => {
                      setRolling(deploy);
                    }
                  : null
              }
              onUndeploy={
                deployment !== undefined && deploy.actions.includes("undeploy")
                  ? () => {
                      setUndeploying(deployment);
                    }
                  : null
              }
              heldBy={holding ? noteId : null}
            />
          );
        })}
      </ViewList>
      {/* The status region stays in place, so the note is announced when it arrives. */}
      <div className="jobs-rollback-note" id={noteId} role="status" tabIndex={-1} ref={note}>
        {undeployNote !== null && (
          <p>
            {t("jobs.deploys.undeployNote", {
              repository: undeployNote.repository,
              environment,
            })}
          </p>
        )}
        {rolled !== null && (
          <RollbackNote
            key={rolled.id}
            environment={environment}
            repository={rolled.repository}
            commit={rolled.commit}
            deployId={rolled.id}
            onEnded={() => {
              setEndedId(rolled.id);
              onEnded();
            }}
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
              setUndeployNote(null);
              setRolled({
                id,
                repository: rolling.repository,
                commit: rolling.resolved_sha.slice(0, 7),
              });
              setRolling(null);
            }}
            onCancel={() => {
              setRolling(null);
            }}
          />
        </Suspense>
      )}
      {undeploying !== null && (
        <Suspense fallback={null}>
          <UndeployDialog
            environment={environment}
            repository={undeploying.repository}
            projectVersionId={undeploying.project_version_id}
            runAs={undeploying.run_as}
            protectedEnvironment={protectedEnvironment}
            onDone={() => {
              const { repository } = undeploying;
              setRolled(null);
              setUndeployNote({ repository });
              setUndeployed({
                repositories: [...gone, repository],
                before: deployments.data,
              });
              setUndeploying(null);
              onEnded();
            }}
            onCancel={() => {
              setUndeploying(null);
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
