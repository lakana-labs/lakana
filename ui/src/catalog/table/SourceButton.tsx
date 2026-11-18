/**
 * Show source: a button that opens the file that defines a table in a dialog, with where the
 * file is, the defining line marked and the server's link to the git host. The file is read only
 * when the dialog opens; closing it puts the focus back on the button. Defined in on the table's
 * page and a failed task's controls both use it.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useCallback, useEffect, useRef } from "react";
import { Button, Dialog, DialogTrigger, Heading, Modal, ModalOverlay } from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  getDefinitionFile,
  type TableDefinitionFileOut,
  type TableDefinitionOut,
  type TableRef,
} from "../../api";
import { useResource } from "../../shell/useResource";
import { markedText, VisibleText } from "../../shell/VisibleText";
import "./tableStrings";
import { ReadFailed } from "../ReadFailed";

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
export function locationOf(answer: TableDefinitionOut): Location | null {
  const { defined, repository, path, commit, line, browse_url: browse } = answer;
  if (!defined || repository === null || path === null || commit === null || line === null) {
    return null;
  }
  return { repository, path, commit, line, link: httpsLink(browse) };
}

/**
 * A link of the server's, kept only when it is an HTTPS address.
 *
 * @param url - The server's link, or null.
 * @returns The link, or null when there is none or it is not an HTTPS address.
 */
function httpsLink(url: string | null): string | null {
  return url !== null && url.startsWith("https://") && URL.canParse(url) ? url : null;
}

/** Properties of {@link HostLink}. */
export interface HostLinkProps {
  /** The server's link, or null. */
  readonly url: string | null;
  /** What the link says, before its arrow. */
  readonly label: string;
  /** Text before the link, drawn only when the link is. */
  readonly before?: string | undefined;
}

/**
 * The server's link to the file on the git host, opened in a new tab without an opener or a
 * referrer. Nothing is drawn unless the link is an HTTPS address.
 *
 * @param props - The link.
 * @param props.url - The server's link, or null.
 * @param props.label - What the link says.
 * @param props.before - Text drawn before the link.
 * @returns The link, or nothing.
 */
export function HostLink({ url, label, before }: HostLinkProps): JSX.Element | null {
  const { t } = useTranslation();
  const href = httpsLink(url);
  if (href === null) {
    return null;
  }
  return (
    <>
      {before}
      <a href={href} target="_blank" rel="noopener noreferrer">
        {label}
        <span aria-hidden="true"> ↗</span>
        <span className="visually-hidden"> {t("home.newTab")}</span>
      </a>
    </>
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
        <p>
          <VisibleText text={file.data.message ?? ""} />
        </p>
      ) : (
        <SourceText content={file.data.content} line={where.line} />
      )}
      {where.link !== null && (
        <p>
          <HostLink url={where.link} label={t("table.definedIn.openHost")} />
        </p>
      )}
    </>
  );
}

/** Properties of {@link SourceButton}. */
export interface SourceButtonProps {
  /** The table whose defining file the dialog shows. */
  readonly place: TableRef;
  /** What the button says. */
  readonly label: ReactNode;
  /**
   * The button's class: `link-button` (a link in a sentence, as Defined in shows it) by default,
   * `button` beside other buttons, as a task's controls show it.
   */
  readonly className?: "link-button" | "button";
}

/**
 * The button that opens the table's defining file in a dialog. The file is read only when the
 * dialog opens, and closing the dialog puts the focus back on the button.
 *
 * @param props - The table and the button's label.
 * @param props.place - The table.
 * @param props.label - What the button says.
 * @param props.className - The button's class.
 * @returns The button and its dialog.
 */
export function SourceButton({
  place,
  label,
  className = "link-button",
}: SourceButtonProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <DialogTrigger>
      <Button className={className}>{label}</Button>
      <ModalOverlay className="palette-overlay" isDismissable>
        <Modal className="float dialog source-dialog">
          <Dialog className="palette-dialog">
            {({ close }) => (
              <>
                <Heading slot="title" level={2}>
                  {t("table.definedIn.title", { name: markedText(place.name) })}
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
  );
}
