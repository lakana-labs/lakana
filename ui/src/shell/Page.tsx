/**
 * The frame of every page in the working area: its heading and the document title.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, type Ref, useEffect } from "react";
import { useTranslation } from "react-i18next";

/** Properties of {@link Page}. */
export interface PageProps {
  /** The page's name: its heading and the first part of the document title. */
  readonly title: string;
  /** The page's content under the heading. */
  readonly children: ReactNode;
  /** The page's one primary action, shown top right beside the heading; none by default. */
  readonly action?: ReactNode;
  /** A ref to the heading, for a page that moves focus there after a change; none by default. */
  readonly headingRef?: Ref<HTMLHeadingElement> | undefined;
}

/**
 * Sets `document.title` to "<title> · Lakana" and renders the heading, which the shell
 * focuses after a navigation (hence `tabIndex={-1}`).
 *
 * @param props - The title, the content and the action.
 * @param props.title - The page's name.
 * @param props.children - The content under the heading.
 * @param props.action - The page's one primary action.
 * @param props.headingRef - A ref to the heading.
 * @returns The page.
 */
export function Page({ title, children, action, headingRef }: PageProps): JSX.Element {
  const { t } = useTranslation();
  useEffect(() => {
    document.title = t("shell.documentTitle", { page: title });
  }, [t, title]);
  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title" tabIndex={-1} ref={headingRef}>
          {title}
        </h1>
        {action}
      </div>
      {children}
    </div>
  );
}
