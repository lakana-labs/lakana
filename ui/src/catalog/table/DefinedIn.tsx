/**
 * The Facts' Defined in: the file, line and commit that define the table, with the server's link
 * to the git host, and the file's text in a dialog when the person asks for it.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useRef } from "react";
import { Button, Dialog, DialogTrigger, Heading, Modal, ModalOverlay } from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  getDefinition,
  getDefinitionFile,
  type TableDefinitionFileOut,
  type TableDefinitionOut,
  type TableRef,
} from "../../api";
import { useResource } from "../../shell/useResource";
import { VisibleText } from "../../shell/VisibleText";
import { ReadFailed } from "../cells";

/** How many characters of the commit the fact shows; the dialog shows all of it. */
const SHORT_COMMIT = 7;

/** Properties of {@link DefinedIn}. */
export interface DefinedInProps {
  /** The table. */
  readonly place: TableRef;
}

/** A definition the server found: every field of the location is set. */
interface Location {
  /** The repository's name. */
  readonly repository: string;
  /** The file's path in the repository. */
  readonly path: string;
  /** The deployed commit. */
  readonly commit: string;
  /** The line the file defines the table on. */
  readonly line: number;
  /** The server's link to the line, when it is an HTTPS link. */
  readonly link: string | null;
}

/**
 * The location of an answer, or null for a table no deployment defines. The link is the
 * server's, never built here, and kept only when it is an HTTPS address.
 *
 * @param answer - The server's answer.
 * @returns The location, or null.
 */
function locationOf(answer: TableDefinitionOut): Location | null {
  const { defined, repository, path, commit, line, browse_url: browse } = answer;
  if (!defined || repository === null || path === null || commit === null || line === null) {
    return null;
  }
  const link = browse !== null && browse.startsWith("https://") && URL.canParse(browse);
  return { repository, path, commit, line, link: link ? browse : null };
}

/**
 * The server's link to the line on the git host, opened in a new tab without an opener or a
 * referrer.
 *
 * @param props - The link.
 * @param props.href - The server's HTTPS link.
 * @returns The link.
 */
function HostLink({ href }: { readonly href: string }): JSX.Element {
  const { t } = useTranslation();
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {t("table.definedIn.openHost")}
      <span aria-hidden="true"> ↗</span>
      <span className="visually-hidden"> {t("home.newTab")}</span>
    </a>
  );
}

/** Properties of {@link SourceText}. */
interface SourceTextProps {
  /** The file's text. */
  readonly content: string;
  /** The line that defines the table, marked and scrolled into view. */
  readonly line: number;
}

/**
 * The file's text in its own scrolling region that takes a tab stop: one line per row with a
 * number screen readers skip, the defining line marked, every character through
 * {@link VisibleText}.
 *
 * @param props - The text and the line.
 * @param props.content - The file's text.
 * @param props.line - The line that defines the table.
 * @returns The text.
 */
function SourceText({ content, line }: SourceTextProps): JSX.Element {
  const { t } = useTranslation();
  const marked = useRef<HTMLElement>(null);
  useEffect(() => {
    marked.current?.scrollIntoView({ block: "center" });
  }, []);
  const lines = content.endsWith("\n") ? content.slice(0, -1).split("\n") : content.split("\n");
  return (
    <pre
      className="source-text"
      role="region"
      aria-label={t("table.definedIn.text")}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <code>
        {lines.map((text, index) => {
          const number = index + 1;
          const parts = (
            <>
              <span className="line-number" aria-hidden="true">
                {number}
              </span>
              <VisibleText text={text} />
              {"\n"}
            </>
          );
          return number === line ? (
            <mark key={number} ref={marked}>
              {parts}
            </mark>
          ) : (
            <span key={number}>{parts}</span>
          );
        })}
      </code>
    </pre>
  );
}

/** Properties of {@link FileBody}. */
interface FileBodyProps {
  /** The table. */
  readonly place: TableRef;
}

/**
 * Reads the file when it mounts, that is when the dialog opens, and shows the text, the server's
 * sentence for why it is not shown, or why it could not be read with Try again. A 503 means the
 * read could not be recorded in the audit log, so no text came.
 *
 * @param props - The table.
 * @param props.place - The table.
 * @returns The file's place, and its text or why not.
 */
function FileBody({ place }: FileBodyProps): JSX.Element {
  const { t } = useTranslation();
  const load = useCallback(() => getDefinitionFile(place), [place]);
  const file = useResource<TableDefinitionFileOut>(load);
  if (file.error !== null) {
    const message =
      file.failure?.status === 503 ? t("table.definedIn.auditUnavailable") : file.error;
    return <ReadFailed message={message} onRetry={file.reload} />;
  }
  if (file.data === undefined) {
    return <p role="status">{t("table.definedIn.reading")}</p>;
  }
  const where = locationOf(file.data);
  if (where === null) {
    // A deploy between the two reads can leave the table defined by no project.
    return <p>{t("table.definedIn.gone")}</p>;
  }
  return (
    <>
      <dl className="source-where">
        <div className="fact">
          <dt>{t("table.definedIn.repository")}</dt>
          <dd>
            <VisibleText text={where.repository} />
          </dd>
        </div>
        <div className="fact">
          <dt>{t("table.definedIn.path")}</dt>
          <dd>
            <VisibleText text={where.path} />
          </dd>
        </div>
        <div className="fact">
          <dt>{t("table.definedIn.commit")}</dt>
          <dd>
            <VisibleText text={where.commit} />
          </dd>
        </div>
      </dl>
      {file.data.content === null ? (
        <p>{file.data.message}</p>
      ) : (
        <SourceText content={file.data.content} line={where.line} />
      )}
      {where.link !== null && (
        <p>
          <HostLink href={where.link} />
        </p>
      )}
    </>
  );
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
        <DialogTrigger>
          <Button className="link-button">
            <span className="visually-hidden">{t("table.definedIn.show")} </span>
            <VisibleText text={where.path} />
          </Button>
          <ModalOverlay className="palette-overlay" isDismissable>
            <Modal className="float dialog source-dialog">
              <Dialog className="palette-dialog">
                {({ close }) => (
                  <>
                    <Heading slot="title" level={2}>
                      {t("table.definedIn.title", { name: place.name })}
                    </Heading>
                    <FileBody place={place} />
                    <div className="step-actions">
                      <Button className="button" onPress={close}>
                        {t("table.definedIn.close")}
                      </Button>
                    </div>
                  </>
                )}
              </Dialog>
            </Modal>
          </ModalOverlay>
        </DialogTrigger>
        {t("table.definedIn.line", { line: where.line })}
        <span aria-hidden="true"> · </span>
        <VisibleText text={where.commit.slice(0, SHORT_COMMIT)} />
        {where.link !== null && (
          <>
            {" "}
            <HostLink href={where.link} />
          </>
        )}
      </dd>
    </div>
  );
}
