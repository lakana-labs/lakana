/**
 * Home's Your work, for people who may deploy: their jobs as rows that open them, and their
 * repositories with the last deploy in each environment.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import { useTranslation } from "react-i18next";

import { getWork, type WorkRepositoryOut } from "../api";
import { Loaded } from "../shell/Loaded";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { StatusLabel } from "../status/StatusLabel";
import { STATUS_KINDS, type StatusKind } from "../status/status";
import { EnvironmentLabel } from "./EnvironmentLabel";
import { objectPath } from "./links";
import { ObjectRow, ObjectTable } from "./ObjectRows";

/**
 * Whether a run's or a deploy's state has a glyph.
 *
 * @param state - The state the server named, or null.
 * @returns True for one of the seven states.
 */
function isKind(state: string | null): state is StatusKind {
  return STATUS_KINDS.some((kind) => kind === state);
}

/** Properties of {@link Repository}. */
interface RepositoryProps {
  /** The repository. */
  readonly repository: WorkRepositoryOut;
}

/**
 * One repository: its name, then each environment with the state and time of its last deploy.
 * It has no page of its own yet, so it is not a row that opens.
 *
 * @param props - The repository.
 * @param props.repository - The repository.
 * @returns The list item.
 */
function Repository({ repository }: RepositoryProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const name = useId();
  return (
    <li aria-labelledby={name}>
      <span className="home-repo-name" id={name}>
        {repository.name}
      </span>
      <ul className="home-deploys">
        {repository.environments.map((deploy) => (
          <li key={deploy.environment}>
            <EnvironmentLabel environment={deploy.environment} />
            {isKind(deploy.last_deploy_status) ? (
              <StatusLabel kind={deploy.last_deploy_status} />
            ) : (
              <span>{t("home.work.neverDeployed")}</span>
            )}
            {deploy.last_deploy_at !== null && (
              <span className="home-when">{when(deploy.last_deploy_at)}</span>
            )}
          </li>
        ))}
      </ul>
    </li>
  );
}

/**
 * Lists the person's jobs and repositories; read once when Home opens.
 *
 * @returns The section.
 */
export function YourWork(): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  const work = useResource(getWork);
  return (
    <section className="home-section" aria-labelledby={heading}>
      <h2 id={heading}>{t("home.work.title")}</h2>
      <Loaded resource={work}>
        {(found) =>
          found.jobs.length === 0 && found.repositories.length === 0 ? (
            <p className="empty">{t("home.work.none")}</p>
          ) : (
            <>
              {found.jobs.length > 0 && (
                <ObjectTable column={t("home.work.lastRun")}>
                  {found.jobs.map((job) => (
                    <ObjectRow
                      key={`${job.environment} ${job.repository} ${job.job}`}
                      to={objectPath("job", job.environment, `${job.repository}/${job.job}`)}
                      name={job.job}
                      environment={job.environment}
                      second={job.repository}
                      aside={
                        isKind(job.last_run_status) ? (
                          <StatusLabel kind={job.last_run_status} />
                        ) : (
                          t("home.work.neverRun")
                        )
                      }
                    />
                  ))}
                </ObjectTable>
              )}
              {found.repositories.length > 0 && (
                <ul className="home-repos" aria-label={t("home.work.repositories")}>
                  {found.repositories.map((repository) => (
                    <Repository key={repository.name} repository={repository} />
                  ))}
                </ul>
              )}
            </>
          )
        }
      </Loaded>
    </section>
  );
}
