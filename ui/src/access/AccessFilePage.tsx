/**
 * Settings › Access › Access file: the scopes access files manage, the changes made by hand
 * since, and Download access file.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";

/**
 * Renders Access file.
 *
 * @returns The page.
 */
export function AccessFilePage(): JSX.Element {
  const { t } = useTranslation();
  return <SettingsFrame title={t("settings.places.accessFile")}>{null}</SettingsFrame>;
}
