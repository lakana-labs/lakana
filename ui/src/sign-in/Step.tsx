/**
 * The frame of every sign-in page: the product's name, one heading, and the step's content.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

/** Properties of {@link Step}. */
export interface StepProps {
  /** The step's name: its heading and the first part of the document title. */
  readonly title: string;
  /** The step's content under the heading. */
  readonly children: ReactNode;
}

/**
 * Renders a sign-in step outside the shell. Focus moves to the heading whenever the step
 * changes, so a screen reader announces where the person is now.
 *
 * @param props - The title and the content.
 * @param props.title - The step's name.
 * @param props.children - The content under the heading.
 * @returns The step.
 */
export function Step({ title, children }: StepProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    document.title = t("shell.documentTitle", { page: title });
    heading.current?.focus();
  }, [t, title]);
  return (
    <main className="step">
      <p className="step-product">{t("shell.product")}</p>
      <h1 className="page-title" ref={heading} tabIndex={-1}>
        {title}
      </h1>
      {children}
    </main>
  );
}
