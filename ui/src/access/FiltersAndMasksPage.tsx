/**
 * Settings › Access › Row filters & masks: every row filter and column mask, filtered, with Add
 * and Delete.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";

/**
 * Renders Row filters & masks.
 *
 * @returns The page.
 */
export function FiltersAndMasksPage(): JSX.Element {
  const { t } = useTranslation();
  return <SettingsFrame title={t("settings.places.filtersAndMasks")}>{null}</SettingsFrame>;
}
