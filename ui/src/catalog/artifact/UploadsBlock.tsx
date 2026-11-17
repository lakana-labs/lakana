/**
 * Your open uploads of an artifact, on its Versions tab: the uploads the reader began and did
 * not finish, 100 at a time, each with Withdraw, the route of `lakana artifact uploads list` and
 * `uploads abort`.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  type ArtifactUploadOut,
  listUploads,
  type TableRef,
  withdrawUpload,
} from "../../api";
import type { CursorPage } from "../../api/manage";
import { PendingButton } from "../../shell/PendingButton";
import { markedText, VisibleText } from "../../shell/VisibleText";
import { problemText } from "../../sign-in/messages";
import { useWhen } from "../../shell/useWhen";
import { When } from "../cells";
import { useCursorPages } from "../views/useCursorPages";
import { ViewList } from "../views/ViewList";
import "./artifactStrings";

/** The page a reader who may not list uploads gets: none, so the block shows nothing. */
const NONE: CursorPage<ArtifactUploadOut> = { items: [], nextCursor: null };

/**
 * Reads a page of the reader's open uploads of one artifact. A 403 on the first page reads as no
 * upload: the page is about the artifact, not about uploads, so a reader who may not list them
 * sees nothing. A 403 on a later page is thrown like any refusal: the list says why and keeps
 * Show more, instead of ending as if it were complete.
 *
 * @param environment - The environment.
 * @param artifact - The artifact, as `namespace.name`.
 * @param cursor - The cursor of the page, or null for the first.
 * @returns The page.
 * @throws {ApiError} for any other refusal, and for a 403 on a later page.
 */
async function readUploads(
  environment: string,
  artifact: string,
  cursor: string | null,
): Promise<CursorPage<ArtifactUploadOut>> {
  try {
    return await listUploads(environment, artifact, cursor);
  } catch (error) {
    if (cursor === null && error instanceof ApiError && error.status === 403) {
      return NONE;
    }
    throw error;
  }
}

/** What the last Withdraw came to: the upload withdrawn, or the server's sentence. */
type Outcome =
  | { readonly kind: "withdrew"; readonly id: string }
  | { readonly kind: "refused"; readonly sentence: string };

/** Properties of {@link UploadsBlock}. */
export interface UploadsBlockProps {
  /** The artifact. */
  readonly place: TableRef;
}

/**
 * Lists the reader's own open uploads of the artifact, as the server returns them, and shows
 * nothing while they are read, when there are none, or when the reader may not list them. Another
 * failure shows the server's sentence with Try again. Withdraw acts at once, as the command line
 * asks nothing: the row goes and "Withdrew upload …" takes the focus. A refusal shows the server's
 * sentence with the focus; a 409 (the upload expired or was committed meanwhile) also reads the
 * list again.
 *
 * @param props - The artifact.
 * @param props.place - The artifact.
 * @returns The block, or nothing.
 */
export function UploadsBlock({ place }: UploadsBlockProps): JSX.Element | null {
  const { t } = useTranslation();
  const { environment, namespace, name } = place;
  const artifact = `${namespace}.${name}`;
  const pages = useCursorPages(`${environment}\u0000${artifact}`, (cursor) =>
    readUploads(environment, artifact, cursor),
  );
  const when = useWhen();
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  const [working, setWorking] = useState<ReadonlySet<string>>(new Set());
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const refusal = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    // The row and its button may be gone: the outcome takes the focus, never the page.
    (outcome?.kind === "refused" ? refusal : status).current?.focus();
  }, [outcome]);

  const rows = (pages.items ?? []).filter((row) => !removed.has(row.upload_id));
  const failed = pages.items === undefined && pages.error !== null;
  if (rows.length === 0 && outcome === null && !failed) {
    return null;
  }

  const withdraw = async (id: string): Promise<void> => {
    setWorking((previous) => new Set([...previous, id]));
    try {
      await withdrawUpload(place, id);
      setRemoved((previous) => new Set([...previous, id]));
      setOutcome({ kind: "withdrew", id });
    } catch (error) {
      setOutcome({ kind: "refused", sentence: problemText(t, error) });
      if (error instanceof ApiError && error.status === 409) {
        pages.reload();
      }
    } finally {
      setWorking((previous) => new Set([...previous].filter((other) => other !== id)));
    }
  };

  const dotted = markedText(artifact);
  return (
    <section className="uploads-section flat-panel">
      <h2 className="block-heading">{t("table.artifact.uploads.heading")}</h2>
      <p className="second">{t("table.artifact.uploads.intro")}</p>
      <p className="uploads-result" role="status" tabIndex={-1} ref={status}>
        {outcome?.kind === "withdrew" &&
          t("table.artifact.uploads.withdrew", { id: markedText(outcome.id) })}
      </p>
      {outcome?.kind === "refused" && (
        <p className="step-error" role="alert" tabIndex={-1} ref={refusal}>
          {outcome.sentence}
        </p>
      )}
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={t("table.artifact.uploads.none", { artifact: dotted })}
        region={t("table.artifact.uploads.region", { artifact: dotted })}
        head={
          <tr>
            <th scope="col">{t("table.artifact.uploads.columns.upload")}</th>
            <th scope="col">{t("table.artifact.uploads.columns.began")}</th>
            <th scope="col">{t("table.artifact.uploads.columns.expires")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("table.artifact.uploads.columns.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((row) => (
          <tr key={row.upload_id}>
            <td>
              <span className="upload-id row-name">
                <VisibleText text={row.upload_id} />
              </span>
            </td>
            <td>
              <When iso={row.created_at} never="" />
            </td>
            <td>
              <time dateTime={row.expires_at}>{when(row.expires_at)}</time>
            </td>
            <td>
              <PendingButton
                className="button"
                aria-label={t("table.artifact.uploads.withdrawOf", {
                  id: markedText(row.upload_id),
                })}
                pending={working.has(row.upload_id)}
                onPress={() => {
                  void withdraw(row.upload_id);
                }}
              >
                {t("table.artifact.uploads.withdraw")}
              </PendingButton>
            </td>
          </tr>
        ))}
      </ViewList>
    </section>
  );
}
