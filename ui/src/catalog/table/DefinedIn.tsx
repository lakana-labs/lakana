/**
 * The Facts' Defined in: the file, line and commit that define the table, with the server's link
 * to the git host, and the file's text in a dialog when the person asks for it.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback } from "react";
import { useTranslation } from "react-i18next";

import { getDefinition, type TableRef } from "../../api";
import { useResource } from "../../shell/useResource";
import { VisibleText } from "../../shell/VisibleText";
import { ReadFailed } from "../ReadFailed";
import { HostLink, locationOf, SourceButton } from "./SourceButton";

/** How many characters of the commit the fact shows; the dialog shows all of it. */
const SHORT_COMMIT = 7;

/** Properties of {@link DefinedIn}. */
export interface DefinedInProps {
  /** The table. */
  readonly place: TableRef;
}

/**
 * Renders Defined in as one fact of the Facts list once its read arrives: the path as a button
 * that opens the file, the line and the short commit, and the link to the git host when the
 * server gives an HTTPS one. Without a link the path and commit stay plain text to copy. A table
 * no project defines has no fact. The file is read only when the dialog opens; closing it puts
 * the focus back on the path.
 *
 * @param props - The table.
 * @param props.place - The table.
 * @returns The fact, or nothing.
 */
export function DefinedIn({ place }: DefinedInProps): JSX.Element | null {
  const { t } = useTranslation();
  const load = useCallback(() => getDefinition(place), [place]);
  const definition = useResource(load);
  const where = definition.data === undefined ? null : locationOf(definition.data);
  if (where === null) {
    return definition.error === null ? null : (
      <div className="fact">
        <dt>{t("table.definedIn.term")}</dt>
        <dd>
          <ReadFailed message={definition.error} onRetry={definition.reload} />
        </dd>
      </div>
    );
  }
  return (
    <div className="fact">
      <dt>{t("table.definedIn.term")}</dt>
      <dd>
        <SourceButton
          place={place}
          label={
            <>
              <span className="visually-hidden">{t("table.definedIn.show")} </span>
              <VisibleText text={where.path} />
            </>
          }
        />
        {t("table.definedIn.line", { line: where.line })}
        <span aria-hidden="true"> · </span>
        <VisibleText text={where.commit.slice(0, SHORT_COMMIT)} />
        {where.link !== null && (
          <>
            {" "}
            <HostLink url={where.link} label={t("table.definedIn.openHost")} />
          </>
        )}
      </dd>
    </div>
  );
}
