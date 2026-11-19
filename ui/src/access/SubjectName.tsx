/**
 * Who a grant, row filter or column mask is for.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { SubjectOut } from "../api/table";
import { VisibleText } from "../shell/VisibleText";
import { personPath } from "./personPath";
import "./subjectStrings";

/** Properties of {@link SubjectName}. */
export interface SubjectNameProps {
  /** Who it is for, as the server names them. */
  readonly subject: SubjectOut;
  /** Whether a person or service account links to their page (only an administrator's may). */
  readonly link: boolean;
}

/**
 * Renders a person or service account by name, linked to their page when asked, with "service
 * account" below a service account's; a group as "group" and its name; or, when the subject was
 * deleted while the list was read, words and the id.
 *
 * @param props - The subject and whether to link.
 * @param props.subject - Who it is for.
 * @param props.link - Whether a principal links to their page.
 * @returns The subject.
 */
export function SubjectName({ subject, link }: SubjectNameProps): JSX.Element {
  const { t } = useTranslation();
  if (subject.principal !== null) {
    const name = <VisibleText text={subject.principal.name} breakable />;
    return (
      <>
        {link ? <Link to={personPath(subject.principal.name)}>{name}</Link> : name}
        {subject.principal.principal_type === "service" && (
          <span className="second">{t("access.subject.serviceAccount")}</span>
        )}
      </>
    );
  }
  if (subject.group !== null) {
    return (
      <Trans
        t={t}
        i18nKey="access.subject.group"
        components={{ name: <VisibleText text={subject.group} breakable /> }}
      />
    );
  }
  return (
    <>
      {t("access.subject.removed")} <code>{subject.principal_id ?? subject.group_id}</code>
    </>
  );
}
