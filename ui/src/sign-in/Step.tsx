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
  /** What the heading shows instead of the title, such as a question with names in it. */
  readonly heading?: ReactNode;
  /** The step's content under the heading. */
  readonly children: ReactNode;
}

/**
 * Renders a sign-in step outside the shell. Focus moves to the heading whenever the step
 * changes, so a screen reader announces where the person is now.
 *
 * @param props - The title and the content.
 * @param props.title - The step's name.
 * @param props.heading - What the heading shows instead of the title.
 * @param props.children - The content under the heading.
 * @returns The step.
 */
export function Step({ title, heading, children }: StepProps): JSX.Element {
  const { t } = useTranslation();
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    document.title = t("shell.documentTitle", { page: title });
    headingRef.current?.focus();
  }, [t, title]);
  return (
    <main className="step">
      <p className="step-product">{t("shell.product")}</p>
      <h1 className="page-title" ref={headingRef} tabIndex={-1}>
        {heading ?? title}
      </h1>
      {children}
    </main>
  );
}
