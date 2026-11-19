/**
 * A protected environment's mark beside its name, shared by the environment picker and the
 * environment's label. Its rules are in `environment.css`, which each screen's stylesheet
 * imports.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { EnvironmentChoice } from "../api/environments";
import "./environmentStrings";
import { VisibleText } from "./VisibleText";

/** Properties of {@link EnvironmentName}. */
export interface EnvironmentNameProps {
  /** The environment. */
  readonly environment: EnvironmentChoice;
}

/**
 * An environment's name, and for a protected one the mark: a dot before it and the word
 * "protected" after it, both in the colour of danger, so the colour never stands alone
 * ("● prod · protected").
 *
 * @param props - The environment.
 * @param props.environment - The environment.
 * @returns The name.
 */
export function EnvironmentName({ environment }: EnvironmentNameProps): JSX.Element {
  const { t } = useTranslation();
  if (!environment.protected) {
    return <VisibleText text={environment.name} />;
  }
  return (
    <>
      <span className="env-protected" aria-hidden="true">
        ●
      </span>{" "}
      <VisibleText text={environment.name} />{" "}
      <span className="env-protected">
        <span aria-hidden="true">· </span>
        {t("environment.protected")}
      </span>
    </>
  );
}
