/**
 * Settings → Repositories: where a repository is connected, and later where the connected ones
 * are listed.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-aria-components";

import { SettingsFrame } from "./SettingsFrame";
import "./settingsStrings";

/**
 * Renders the purpose and the way to Connect a repository.
 *
 * @returns The place.
 */
export function RepositoriesPlace(): JSX.Element {
  const { t } = useTranslation();
  return (
    <SettingsFrame title={t("settings.places.repositories")}>
      <p className="page-purpose">{t("settings.repositories.intro")}</p>
      <Link className="button button-primary" href="/settings/repositories/new">
        {t("settings.repositories.connect")}
      </Link>
    </SettingsFrame>
  );
}
