/**
 * An environment's page in Settings › Environments.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";
import { ENVIRONMENTS_PATH } from "./environmentPaths";
import "./settingsStrings";

/** Properties of {@link EnvironmentPage}. */
export interface EnvironmentPageProps {
  /** The environment's name, from the address. */
  readonly name: string;
}

/**
 * Renders the environment's page under Settings › Environments.
 *
 * @param props - The environment's name.
 * @param props.name - The environment's name, from the address.
 * @returns The page.
 */
export function EnvironmentPage({ name }: EnvironmentPageProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page
      title={name}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: ENVIRONMENTS_PATH, label: t("settings.places.environments") },
      ]}
    >
      {null}
    </Page>
  );
}
