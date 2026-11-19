/**
 * The small label that names a row's environment.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { EnvironmentName } from "./EnvironmentName";

/** Properties of {@link EnvironmentLabel}. */
export interface EnvironmentLabelProps {
  /** The environment's name; null for something of the whole instance (a worker, a link). */
  readonly environment: string | null;
  /**
   * Whether the environment is protected, which marks it as the environment picker does; false
   * by default, for a screen that does not know.
   */
  readonly protected?: boolean;
}

/**
 * Names the environment in a quiet label; a screen reader hears "Environment prod". A protected
 * one carries the mark of {@link EnvironmentName} ("● prod · protected"). Something of the whole
 * instance is labelled "Instance".
 *
 * @param props - The environment.
 * @param props.environment - The environment's name, or null.
 * @param props.protected - Whether it is protected.
 * @returns The label.
 */
export function EnvironmentLabel({
  environment,
  protected: isProtected = false,
}: EnvironmentLabelProps): JSX.Element {
  const { t } = useTranslation();
  if (environment === null) {
    return <span className="env-label">{t("home.wholeInstance")}</span>;
  }
  return (
    <span className="env-label">
      <span className="visually-hidden">{t("home.environment")} </span>
      <EnvironmentName environment={{ name: environment, protected: isProtected }} />
    </span>
  );
}
