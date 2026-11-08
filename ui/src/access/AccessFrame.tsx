/**
 * The frame of the Access area's places: its heading, its one action and its tabs.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";
import { SectionTabs } from "../shell/SectionTabs";

/** Properties of {@link AccessFrame}. */
export interface AccessFrameProps {
  /** The place's one primary action, if any. */
  readonly action?: ReactNode;
  /** The place's content. */
  readonly children: ReactNode;
}

/**
 * Renders the Access heading with the tabs People, Invites and Recovery links.
 *
 * @param props - The action and the content.
 * @param props.action - The place's one primary action.
 * @param props.children - The place's content.
 * @returns The frame.
 */
export function AccessFrame({ action, children }: AccessFrameProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page title={t("areas.access.name")} action={action}>
      <SectionTabs
        label={t("access.tabs")}
        tabs={[
          { to: "/access", label: t("access.people.tab") },
          { to: "/access/invites", label: t("access.invites.tab") },
          { to: "/access/recovery-links", label: t("access.recovery.tab") },
        ]}
      />
      {children}
    </Page>
  );
}
