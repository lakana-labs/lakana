/**
 * The frame of the Access area's places: its heading, its one action and its tabs.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode, Ref } from "react";
import { useTranslation } from "react-i18next";

import { Page } from "../shell/Page";
import { SectionTabs } from "../shell/SectionTabs";

/** Properties of {@link AccessFrame}. */
export interface AccessFrameProps {
  /** The place's one primary action, if any. */
  readonly action?: ReactNode;
  /** The place's content. */
  readonly children: ReactNode;
  /** A ref to the heading, to move focus there after a row went away. */
  readonly headingRef?: Ref<HTMLHeadingElement>;
}

/**
 * Renders the Access heading with the tabs People, Invites and Recovery links.
 *
 * @param props - The action and the content.
 * @param props.action - The place's one primary action.
 * @param props.children - The place's content.
 * @param props.headingRef - A ref to the heading.
 * @returns The frame.
 */
export function AccessFrame({ action, children, headingRef }: AccessFrameProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page title={t("areas.access.name")} action={action} headingRef={headingRef}>
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
