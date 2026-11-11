/**
 * The frame of every place in Settings: the area's heading and its tabs.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";
import { SectionTabs } from "../shell/SectionTabs";

/** Properties of {@link SettingsFrame}. */
export interface SettingsFrameProps {
  /** The place's content. */
  readonly children: ReactNode;
}

/**
 * Renders the heading, the tabs Status and Sign-in, and the place.
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
        tabs={[
          { to: "/settings/status", label: t("settings.status.tab") },
          { to: "/settings/sign-in", label: t("settings.signIn.tab") },
        ]}
      />
      {children}
    </Page>
  );
}
