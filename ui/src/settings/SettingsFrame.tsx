/**
 * The frame of every place in Settings: the area's heading and its tabs.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";
import { SectionTabs } from "../shell/SectionTabs";

/** The places of Settings, in tab order; each one is a first-level route of the Settings screen. */
export const SETTINGS_PLACES = ["status", "sign-in", "repositories"] as const;

const TAB_KEYS: Record<(typeof SETTINGS_PLACES)[number], string> = {
  status: "settings.status.tab",
  "sign-in": "settings.signIn.tab",
  repositories: "settings.repositories.tab",
};

/** Properties of {@link SettingsFrame}. */
export interface SettingsFrameProps {
  /** The place's content. */
  readonly children: ReactNode;
}

/**
 * Renders the heading, the tabs Status, Sign-in and Repositories, and the place.
 *
 * @param props - The content.
 * @param props.children - The place's content.
 * @returns The page.
 */
export function SettingsFrame({ children }: SettingsFrameProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page title={t("areas.settings.name")}>
      <SectionTabs
        label={t("settings.tabs")}
        tabs={SETTINGS_PLACES.map((place) => ({
          to: `/settings/${place}`,
          label: t(TAB_KEYS[place]),
        }))}
      />
      {children}
    </Page>
  );
}
