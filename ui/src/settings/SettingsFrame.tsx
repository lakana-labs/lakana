/**
 * The frame of every place in Settings: the place's own page, named after it.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode, Ref } from "react";

import { Page } from "../shell/Page";

/** Properties of {@link SettingsFrame}. */
export interface SettingsFrameProps {
  /** The place's name: its heading and the document title. */
  readonly title: string;
  /** The place's content. */
  readonly children: ReactNode;
  /** The place's one primary action, top right beside the heading; none by default. */
  readonly action?: ReactNode;
  /** A ref to the heading, for a place that moves the focus there after a change; none by default. */
  readonly headingRef?: Ref<HTMLHeadingElement>;
}

/**
 * Renders the place as a page under its name. The list of places stands beside it, drawn by
 * the Settings layout, so the page itself carries no tabs and no breadcrumb.
 *
 * @param props - The name, the content, the action and the heading's ref.
 * @param props.title - The place's name.
 * @param props.children - The place's content.
 * @param props.action - The place's one primary action.
 * @param props.headingRef - A ref to the heading.
 * @returns The page.
 */
export function SettingsFrame({
  title,
  children,
  action,
  headingRef,
}: SettingsFrameProps): JSX.Element {
  return (
    <Page title={title} action={action} headingRef={headingRef}>
      {children}
    </Page>
  );
}
