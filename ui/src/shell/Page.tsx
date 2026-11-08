/**
 * The frame of every page in the working area: its heading and the document title.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useEffect } from "react";
import { useTranslation } from "react-i18next";

/** Properties of {@link Page}. */
export interface PageProps {
  /** The page's name: its heading and the first part of the document title. */
  readonly title: string;
  /** The page's content under the heading. */
  readonly children: ReactNode;
}

/**
 * Sets `document.title` to "<title> · Lakana" and renders the heading, which the shell
 * focuses after a navigation (hence `tabIndex={-1}`).
 *
 * @param props - The title and the content.
 * @param props.title - The page's name.
 * @param props.children - The content under the heading.
 * @returns The page.
 */
export function Page({ title, children }: PageProps): JSX.Element {
  const { t } = useTranslation();
  useEffect(() => {
    document.title = t("shell.documentTitle", { page: title });
  }, [t, title]);
  return (
    <div className="page">
      <h1 className="page-title" tabIndex={-1}>
        {title}
      </h1>
      {children}
    </div>
  );
}
