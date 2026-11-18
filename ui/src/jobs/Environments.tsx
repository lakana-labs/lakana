/**
 * What the Jobs area's tabs show before their lists: the environments the person may read, the
 * wait while they are read, the server's sentence when they cannot be, and how to get access
 * without any.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { type EnvironmentChoice, listEnvironments } from "../api/environments";
import { ReadFailed } from "../catalog/ReadFailed";
import { CopyLine } from "../shell/CopyLine";
import { useResource } from "../shell/useResource";
import "./jobStrings";
import "./listStrings";
import { Reading } from "./Reading";

/** The command that lists an environment's jobs. */
const CLI_JOBS = "lakana job list --env <environment>";

/** Properties of {@link Environments}. */
export interface EnvironmentsProps {
  /** What the tab shows once there is at least one environment. */
  readonly children: (environments: readonly EnvironmentChoice[]) => ReactNode;
}

/**
 * Reads the environments the person may read and renders the tab over them. While they are read
 * it shows the skeleton, a failed read the server's sentence with Try again, and with no
 * environment to read it says how to get access.
 *
 * @param props - The tab.
 * @param props.children - Renders the tab over the environments.
 * @returns The tab, or the state before it.
 */
export function Environments({ children }: EnvironmentsProps): JSX.Element {
  const { t } = useTranslation();
  const environments = useResource(listEnvironments);
  const listed = environments.data;
  if (listed === undefined) {
    return environments.error === null ? (
      <Reading label={t("jobs.list.loading")} />
    ) : (
      <ReadFailed message={environments.error} onRetry={environments.reload} />
    );
  }
  if (listed.length === 0) {
    return (
      <div className="jobs-no-access">
        <p>{t("jobs.list.noEnvironment")}</p>
        <p>{t("jobs.list.noEnvironmentCli")}</p>
        <CopyLine text={CLI_JOBS} copyLabel={t("jobs.list.copy")} />
      </div>
    );
  }
  return <>{children(listed)}</>;
}
