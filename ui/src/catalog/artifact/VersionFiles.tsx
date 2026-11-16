/**
 * The files of one artifact version, under its row on the Versions tab: the command that
 * downloads the whole version, and each file with its size and Download.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  type ArtifactFileOut,
  BROWSER_DOWNLOAD_LIMIT,
  DownloadMismatch,
  downloadFile,
  getVersion,
  type TableRef,
} from "../../api";
import { CopyLine } from "../../shell/CopyLine";
import { PendingButton } from "../../shell/PendingButton";
import { shellQuote } from "../../shell/shellQuote";
import { useResource } from "../../shell/useResource";
import { markedText, VisibleText } from "../../shell/VisibleText";
import { problemText } from "../../sign-in/messages";
import { ReadFailed } from "../cells";
import { formatBytes } from "../format";
import { ListSkeleton } from "../ListSkeleton";
import "./artifactStrings";
import { fileName, saveBlob } from "./saveFile";
import { ShowMore, useShowMore } from "./ShowMore";

/** Properties of {@link VersionFiles}. */
export interface VersionFilesProps {
  /** The artifact. */
  readonly place: TableRef;
  /** The version's number. */
  readonly version: number;
  /** The id of the files' element, which the version's Files button controls. */
  readonly id: string;
  /**
   * Called with the server's sentence when the version turns out to be gone (404), so the
   * versions are read again.
   */
  readonly onGone: (sentence: string) => void;
}

/**
 * The command that downloads a whole version, every part quoted for the shell.
 *
 * @param place - The artifact.
 * @param version - The version's number.
 * @returns The command line.
 */
function wholeVersionCommand(place: TableRef, version: number): string {
  const { environment, namespace, name } = place;
  return [
    "lakana artifact download",
    shellQuote(`${namespace}.${name}`),
    shellQuote(`./${name}-v${String(version)}`),
    `--version ${String(version)}`,
    `--env ${shellQuote(environment)}`,
  ].join(" ");
}

/**
 * Reads the version's files and lists them, 100 at a time. A file up to
 * {@link BROWSER_DOWNLOAD_LIMIT} has Download, which reads it, checks its size and SHA-256
 * against the manifest and only then saves it under the last part of its path; a larger one
 * points to the command. A file that does not arrive whole saves nothing and says so; a refusal
 * shows the server's sentence beside the file, and a 404 also tells `onGone`.
 *
 * @param props - The version.
 * @param props.place - The artifact.
 * @param props.version - The version's number.
 * @param props.id - The id of the files' element.
 * @param props.onGone - Called when the version is gone.
 * @returns The files.
 */
export function VersionFiles({ place, version, id, onGone }: VersionFilesProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const { environment, namespace, name } = place;
  const load = useCallback(
    () => getVersion({ environment, namespace, name }, version),
    [environment, namespace, name, version],
  );
  const resource = useResource(load);
  const files = resource.data?.files ?? [];
  const region = useRef<HTMLDivElement>(null);
  const rows = useShowMore(files.length, region, ".file-path");
  const [working, setWorking] = useState<ReadonlySet<string>>(new Set());
  const [notes, setNotes] = useState<Readonly<Record<string, string>>>({});

  const gone = resource.failure?.status === 404 ? resource.error : null;
  useEffect(() => {
    if (gone !== null) {
      onGone(gone);
    }
    // Told once per answer, not on every render of the caller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gone]);

  const download = async (file: ArtifactFileOut): Promise<void> => {
    setWorking((previous) => new Set([...previous, file.path]));
    setNotes((previous) => ({ ...previous, [file.path]: "" }));
    try {
      saveBlob(await downloadFile(place, version, file), fileName(file.path));
    } catch (error) {
      // A cut connection breaks the body's stream with a TypeError: the file did not arrive whole.
      const broken = error instanceof DownloadMismatch || error instanceof TypeError;
      const sentence = broken
        ? t("table.artifact.files.mismatch", { path: markedText(file.path) })
        : problemText(t, error);
      setNotes((previous) => ({ ...previous, [file.path]: sentence }));
      if (error instanceof ApiError && error.status === 404) {
        onGone(sentence);
      }
    } finally {
      setWorking((previous) => new Set([...previous].filter((path) => path !== file.path)));
    }
  };

  if (resource.data === undefined) {
    return (
      <div id={id} className="version-files">
        {resource.error === null ? (
          <ListSkeleton />
        ) : (
          <ReadFailed message={resource.error} onRetry={resource.reload} />
        )}
      </div>
    );
  }
  return (
    <div id={id} className="version-files">
      <p className="second">{t("table.artifact.files.whole")}</p>
      <CopyLine
        text={wholeVersionCommand(place, version)}
        copyLabel={t("table.artifact.files.copy")}
      />
      {/* Inside the versions' scrolling region: it scrolls with them, so it takes no tab stop. */}
      <div
        className="files-list"
        role="region"
        aria-label={t("table.artifact.files.region", { version })}
        ref={region}
      >
        <table className="list list-static">
          <thead>
            <tr>
              <th scope="col">{t("table.artifact.files.columns.path")}</th>
              <th scope="col" className="number">
                {t("table.artifact.files.columns.size")}
              </th>
              <th scope="col">
                <span className="visually-hidden">
                  {t("table.artifact.files.columns.download")}
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            {files.slice(0, rows.shown).map((file) => {
              const size = formatBytes(file.size, i18n.language);
              const path = markedText(file.path);
              const pending = working.has(file.path);
              const note = notes[file.path] ?? "";
              return (
                <tr key={file.path}>
                  <td>
                    <span className="file-path" tabIndex={-1}>
                      <VisibleText text={file.path} breakable />
                    </span>
                    {note !== "" && (
                      <p className="step-error" role="alert">
                        {note}
                      </p>
                    )}
                  </td>
                  <td className="number">{size}</td>
                  <td>
                    {file.size > BROWSER_DOWNLOAD_LIMIT ? (
                      <span className="second">{t("table.artifact.files.tooLarge", { size })}</span>
                    ) : (
                      <PendingButton
                        className="button"
                        aria-label={t(
                          pending
                            ? "table.artifact.files.downloadingFor"
                            : "table.artifact.files.downloadFor",
                          { path },
                        )}
                        pending={pending}
                        onPress={() => {
                          void download(file);
                        }}
                      >
                        {t(
                          pending
                            ? "table.artifact.files.downloading"
                            : "table.artifact.files.download",
                        )}
                      </PendingButton>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <ShowMore rows={rows} />
    </div>
  );
}
