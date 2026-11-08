/**
 * A new link, shown once in its dialog: copy it, read what it does, close.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { CopyLine } from "../shell/CopyLine";

/** Properties of {@link ShownOnce}. */
export interface ShownOnceProps {
  /** The dialog's title, naming whom to send the link to. */
  readonly title: string;
  /** The link. */
  readonly url: string;
  /** What the link does and what to do before and after sending it. */
  readonly children: ReactNode;
  /** Closes the dialog; the link is gone from the page afterwards. */
  readonly onDone: () => void;
}

/**
 * Renders the link with a copy button and the advice. The page keeps the link only while this
 * is on screen, never in any storage.
 *
 * @param props - The title, the link, the advice and the close handler.
 * @param props.title - The dialog's title.
 * @param props.url - The link.
 * @param props.children - The advice.
 * @param props.onDone - Closes the dialog.
 * @returns The content of the dialog.
 */
export function ShownOnce({ title, url, children, onDone }: ShownOnceProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <Heading slot="title">{title}</Heading>
      <CopyLine text={url} copyLabel={t("access.copyLink")} />
      {children}
      <div className="step-actions">
        {/* The button that opened this content is gone; focus lands on the way out. */}
        {/* eslint-disable-next-line jsx-a11y/no-autofocus */}
        <Button className="button button-primary" onPress={onDone} autoFocus>
          {t("dialog.done")}
        </Button>
      </div>
    </>
  );
}
