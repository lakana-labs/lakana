/**
 * A repository's page in Settings › Repositories.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";
import { REPOSITORIES_PATH } from "./repositoryPaths";
import "./settingsStrings";

/** Properties of {@link RepositoryPage}. */
export interface RepositoryPageProps {
  /** The repository's name, from the address. */
  readonly name: string;
}

/**
 * Renders the repository's page under Settings › Repositories.
 *
 * @param props - The repository's name.
 * @param props.name - The repository's name, from the address.
 * @returns The page.
 */
export function RepositoryPage({ name }: RepositoryPageProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page
      title={name}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: REPOSITORIES_PATH, label: t("settings.places.repositories") },
      ]}
    >
      {null}
    </Page>
  );
}
