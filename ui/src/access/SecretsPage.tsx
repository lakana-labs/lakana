/**
 * Settings › Access › Secrets: an environment's secrets, their names and uses, never values.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { SettingsFrame } from "../settings/SettingsFrame";

/**
 * Renders the place.
 *
 * @returns The place.
 */
export function SecretsPage(): JSX.Element {
  const { t } = useTranslation();
  return <SettingsFrame title={t("settings.places.secrets")}>{null}</SettingsFrame>;
}
