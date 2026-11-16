/**
 * An artifact's Versions tab: every version, newest first and deleted ones included, 100 at a
 * time, with who stored it and when, its size and files, and under a live version its files with
 * Download and Delete. Its code loads only when the tab is opened.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { type ArtifactVersionOut, listVersions } from "../../api";
import { useResource } from "../../shell/useResource";
import { markedText, VisibleText } from "../../shell/VisibleText";
import { ReadFailed, When } from "../cells";
import { formatBytes } from "../format";
import { ListSkeleton } from "../ListSkeleton";
import { useTableData } from "../table/TableContext";
import "./artifactStrings";
import { DeleteVersionDialog } from "./DeleteVersionDialog";
import { ShowMore, useShowMore } from "./ShowMore";
import { VersionFiles } from "./VersionFiles";

/** How many characters of a commit the Stored column shows, as git's short form. */
const SHORT_SHA = 7;

/** The columns of the list, for the files' row that spans them. */
const COLUMNS = 5;

/** A version a download found gone, with the server's sentence. */
interface Gone {
  /** The version's number. */
  readonly version: number;
  /** The server's sentence. */
  readonly sentence: string;
  /** The versions as read when the download found it gone; only a newer read can show it. */
  readonly base: readonly ArtifactVersionOut[] | undefined;
}

/** Where the focus goes once the delete dialog closes. */
interface FocusAfterDelete {
  /** The version's number. */
  readonly version: number;
  /** Its Delete button, after a Cancel; else, or when the button is gone, its number. */
  readonly on: "delete" | "row";
}

/** Properties of {@link Stored}. */
interface StoredProps {
  /** The version. */
  readonly row: ArtifactVersionOut;
}

/**
 * When a version was stored, and below it who stored it and from which commit, when known.
 *
 * @param props - The version.
 * @param props.row - The version.
 * @returns The cell's content.
 */
function Stored({ row }: StoredProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <When iso={row.created_at} never="" />
      {row.created_by !== null && (
        <span className="second">
          {t("table.artifact.versions.by", { who: markedText(row.created_by) })}
        </span>
      )}
      {row.commit_sha !== null && (
        <span className="second">
          {t("table.artifact.versions.commit", {
            sha: markedText(row.commit_sha.slice(0, SHORT_SHA)),
          })}
        </span>
      )}
    </>
  );
}

/**
 * Reads the versions and lists them. Files opens one version's files at a time below its row.
 * When a download finds its version gone, the versions are read again; once the list shows that
 * version deleted, its files close and the server's sentence stands in its row with the focus. A
 * newer read that shows the version live forgets the sentence. Delete… asks first; Close then
 * reads the versions and the artifact again, so "current" moves, and the focus goes to the
 * version's number. Cancel gives it back to Delete…, or to the number once the version is gone.
 *
 * @returns The tab.
 */
export default function VersionsTab(): JSX.Element | null {
  const { t, i18n } = useTranslation();
  const { ref, artifact, reloadArtifact } = useTableData();
  const { environment, namespace, name } = ref;
  const load = useCallback(
    () => listVersions({ environment, namespace, name }),
    [environment, namespace, name],
  );
  const resource = useResource(load);
  const versions = resource.data ?? [];
  const region = useRef<HTMLDivElement>(null);
  const rows = useShowMore(versions.length, region, ".version-number");
  const [open, setOpen] = useState<number | null>(null);
  const [gone, setGone] = useState<Gone | null>(null);
  const goneNote = useRef<HTMLParagraphElement>(null);
  const [deleting, setDeleting] = useState<ArtifactVersionOut | null>(null);
  const focusAfterDelete = useRef<FocusAfterDelete | null>(null);
  const ids = useId();
  const dotted = markedText(`${namespace}.${name}`);
  const number = new Intl.NumberFormat(i18n.language);

  const isDeleted = (version: number): boolean =>
    versions.some((row) => row.version === version && row.deleted_at !== null);
  const goneRead = gone !== null && resource.data !== gone.base;
  if (goneRead && !isDeleted(gone.version)) {
    // A newer read shows the version live, so the sentence stays beside its file and is forgotten
    // here; a later delete of the version must not show it or take the focus.
    setGone(null);
  }
  const goneShown = goneRead && isDeleted(gone.version);
  useEffect(() => {
    if (goneShown) {
      // The files and the button that had the focus are gone with the version.
      goneNote.current?.focus();
    }
  }, [goneShown]);
  const deleteId = (version: number): string => `${ids}-delete-${String(version)}`;
  const numberId = (version: number): string => `${ids}-version-${String(version)}`;
  useEffect(() => {
    const next = focusAfterDelete.current;
    if (deleting !== null || next === null) {
      return;
    }
    focusAfterDelete.current = null;
    const button = next.on === "delete" ? document.getElementById(deleteId(next.version)) : null;
    (button ?? document.getElementById(numberId(next.version)))?.focus();
  });

  if (resource.data === undefined) {
    return (
      <div className="versions-tab">
        <section className="versions-section flat-panel">
          <h2 className="block-heading">{t("table.artifact.versions.heading")}</h2>
          {resource.error === null ? (
            <ListSkeleton />
          ) : (
            <ReadFailed message={resource.error} onRetry={resource.reload} />
          )}
        </section>
      </div>
    );
  }

  const filesId = (version: number): string => `${ids}-files-${String(version)}`;
  const renderRow = (row: ArtifactVersionOut): JSX.Element[] => {
    const live = row.deleted_at === null;
    const expanded = live && open === row.version;
    const cells = (
      <tr key={row.version}>
        <td>
          <span className="version-number row-name" id={numberId(row.version)} tabIndex={-1}>
            {String(row.version)}
          </span>
          {row.version === artifact?.current_version && (
            <span className="second">{t("table.artifact.versions.current")}</span>
          )}
        </td>
        <td>
          <Stored row={row} />
        </td>
        <td className="number">{formatBytes(row.size_bytes, i18n.language)}</td>
        <td className="number">{number.format(row.file_count)}</td>
        <td>
          {live ? (
            <span className="version-actions">
              <button
                type="button"
                className="button"
                aria-expanded={expanded}
                aria-controls={filesId(row.version)}
                aria-label={t("table.artifact.versions.filesOf", { version: row.version })}
                onClick={() => {
                  setOpen(expanded ? null : row.version);
                }}
              >
                {t("table.artifact.versions.files")}
              </button>
              {row.actions.includes("delete") && (
                <button
                  type="button"
                  className="button"
                  id={deleteId(row.version)}
                  aria-label={t("table.artifact.versions.deleteOf", { version: row.version })}
                  onClick={() => {
                    if (gone?.version === row.version) {
                      // A download's sentence about this version is consumed: the delete says its own.
                      setGone(null);
                    }
                    setDeleting(row);
                  }}
                >
                  {t("table.artifact.versions.delete")}
                </button>
              )}
            </span>
          ) : (
            <>
              <Trans
                t={t}
                i18nKey={
                  row.deleted_by === null
                    ? "table.artifact.versions.deleted"
                    : "table.artifact.versions.deletedBy"
                }
                components={{
                  when: <When iso={row.deleted_at} never="" />,
                  who: <VisibleText text={row.deleted_by ?? ""} />,
                }}
              />
              {goneShown && gone.version === row.version && (
                <p className="step-error" role="alert" tabIndex={-1} ref={goneNote}>
                  {gone.sentence}
                </p>
              )}
            </>
          )}
        </td>
      </tr>
    );
    if (!expanded) {
      return [cells];
    }
    return [
      cells,
      <tr key={`${String(row.version)}-files`} className="version-files-row">
        <td colSpan={COLUMNS}>
          <VersionFiles
            place={ref}
            version={row.version}
            id={filesId(row.version)}
            onGone={(sentence) => {
              setGone({ version: row.version, sentence, base: resource.data });
              resource.reload();
            }}
          />
        </td>
      </tr>,
    ];
  };

  return (
    <div className="versions-tab">
      <section className="versions-section flat-panel">
        <h2 className="block-heading">{t("table.artifact.versions.heading")}</h2>
        {versions.length === 0 ? (
          <p className="second">{t("table.artifact.versions.none", { artifact: dotted })}</p>
        ) : (
          <div
            className="list-scroll versions-scroll"
            role="region"
            aria-label={t("table.artifact.versions.region", { artifact: dotted })}
            // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
            tabIndex={0}
            ref={region}
          >
            <table className="list list-static">
              <thead>
                <tr>
                  <th scope="col">{t("table.artifact.versions.columns.version")}</th>
                  <th scope="col">{t("table.artifact.versions.columns.stored")}</th>
                  <th scope="col" className="number">
                    {t("table.artifact.versions.columns.size")}
                  </th>
                  <th scope="col" className="number">
                    {t("table.artifact.versions.columns.files")}
                  </th>
                  <th scope="col">
                    <span className="visually-hidden">
                      {t("table.artifact.versions.columns.actions")}
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>{versions.slice(0, rows.shown).flatMap(renderRow)}</tbody>
            </table>
          </div>
        )}
        {resource.error !== null && (
          <p className="step-error" role="alert">
            {resource.error}
          </p>
        )}
        <ShowMore rows={rows} />
      </section>
      {deleting !== null && (
        <DeleteVersionDialog
          place={ref}
          version={deleting}
          onGone={resource.reload}
          onCancel={() => {
            focusAfterDelete.current = { version: deleting.version, on: "delete" };
            setDeleting(null);
          }}
          onDone={() => {
            focusAfterDelete.current = { version: deleting.version, on: "row" };
            setDeleting(null);
            resource.reload();
            reloadArtifact();
          }}
        />
      )}
    </div>
  );
}
