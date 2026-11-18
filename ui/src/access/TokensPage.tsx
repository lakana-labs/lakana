/**
 * Settings › Access › Tokens: everyone's live personal tokens and the live task tokens.
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
export function TokensPage(): JSX.Element {
  const { t } = useTranslation();
  return <SettingsFrame title={t("settings.places.tokens")}>{null}</SettingsFrame>;
}
