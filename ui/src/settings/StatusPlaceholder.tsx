/**
 * Settings → Status until part 8 brings it: what it will show, and the command that shows it
 * now.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { ComingSoon } from "../shell/ComingSoon";
import { SettingsFrame } from "./SettingsFrame";

/**
 * Renders the placeholder: the purpose, then part 8 and `lakana status` to copy.
 *
 * @returns The place.
 */
export function StatusPlaceholder(): JSX.Element {
  const { t } = useTranslation();
  return (
    <SettingsFrame>
      <p className="page-purpose">{t("settings.status.intro")}</p>
      <ComingSoon part={8} cli="lakana status" />
    </SettingsFrame>
  );
}
