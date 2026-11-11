/**
 * The small label that names a row's environment.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

/** Properties of {@link EnvironmentLabel}. */
export interface EnvironmentLabelProps {
  /** The environment's name; null for something of the whole instance (a worker, a link). */
  readonly environment: string | null;
}

/**
 * Names the environment in a quiet label; a screen reader hears "Environment prod". Something
 * of the whole instance is labelled "Instance".
 *
 * @param props - The environment.
 * @param props.environment - The environment's name, or null.
 * @returns The label.
 */
export function EnvironmentLabel({ environment }: EnvironmentLabelProps): JSX.Element {
  const { t } = useTranslation();
  if (environment === null) {
    return <span className="env-label">{t("home.wholeInstance")}</span>;
  }
  return (
    <span className="env-label">
      <span className="visually-hidden">{t("home.environment")} </span>
      {environment}
    </span>
  );
}
