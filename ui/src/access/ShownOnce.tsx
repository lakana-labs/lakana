/**
 * A new link or token, shown once in its dialog: copy it, read what it does, close.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { Button, Heading } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { CopyLine } from "../shell/CopyLine";

/** Properties of {@link ShownOnce}. */
export interface ShownOnceProps {
  /** The dialog's title, naming whom to send the link to, or the token. */
  readonly title: string;
  /** The link or the token. */
  readonly value: string;
  /** The copy button's label; "Copy link" by default. */
  readonly copyLabel?: string;
  /** What the value does and what to do before and after using it. */
  readonly children: ReactNode;
  /** Closes the dialog; the value is gone from the page afterwards. */
  readonly onDone: () => void;
}

/**
 * Renders the value with a copy button and the advice. The page keeps the value only while
 * this is on screen, never in any storage.
 *
 * @param props - The title, the value, the copy label, the advice and the close handler.
 * @param props.title - The dialog's title.
 * @param props.value - The link or the token.
 * @param props.copyLabel - The copy button's label.
 * @param props.children - The advice.
 * @param props.onDone - Closes the dialog.
 * @returns The content of the dialog.
 */
export function ShownOnce({
  title,
  value,
  copyLabel,
  children,
  onDone,
}: ShownOnceProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <Heading slot="title">{title}</Heading>
      <CopyLine text={value} copyLabel={copyLabel ?? t("access.copyLink")} />
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
