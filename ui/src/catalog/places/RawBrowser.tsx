/**
 * Browse… of New external table: the folders and files under `raw/`, one folder at a time, so the
 * person picks the location instead of typing it. Loaded when Browse… is first pressed.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { listRaw, type RawEntryOut } from "../../api";
import { VisibleText } from "../../shell/VisibleText";
import { formatBytes } from "../format";
import { useCursorPages } from "../views/useCursorPages";
import { ViewList } from "../views/ViewList";
import { locationForFile, locationForFolder } from "./rawLocation";
import "./placeStrings";
import "./rawBrowser.css";

/** The folder the panel opens on. */
const RAW = "raw";

/** Properties of {@link RawBrowser}. */
export interface RawBrowserProps {
  /** The environment whose `raw/` is listed. */
  readonly environment: string;
  /** Called with the location a choice gives, such as `raw/orders/*.csv`. */
  readonly onChoose: (location: string) => void;
}

/** Properties of {@link InUse}. */
interface InUseProps {
  /** The entry. */
  readonly entry: RawEntryOut;
}

/**
 * Says which external table holds an entry: by name when the person may read it.
 *
 * @param props - The entry.
 * @param props.entry - The entry.
 * @returns The words, or nothing for a free entry.
 */
function InUse({ entry }: InUseProps): JSX.Element | null {
  const { t } = useTranslation();
  if (entry.claimed_by === null) {
    return null;
  }
  const { table } = entry.claimed_by;
  return (
    <>
      {table === null
        ? t("catalog.places.external.inUse")
        : t("catalog.places.external.inUseBy", { table })}
    </>
  );
}

/**
 * Renders the panel: the trail of folders, the hint, the listing with Show more, and Use this
 * folder below `raw/`. A folder opens; a file gives its folder and ending; a folder or file in
 * an external table's folder cannot be chosen and says whose it is. A file directly in `raw/`
 * cannot be chosen, as a table needs a folder below it. The server decides the location again
 * when the table is registered.
 *
 * @param props - The environment and the handler.
 * @param props.environment - The environment.
 * @param props.onChoose - Called with the chosen location.
 * @returns The panel.
 */
export default function RawBrowser({ environment, onChoose }: RawBrowserProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const [folder, setFolder] = useState(RAW);
  const load = useCallback(
    (cursor: string | null) => listRaw(environment, folder, cursor),
    [environment, folder],
  );
  const pages = useCursorPages(`${environment}\n${folder}`, load);
  const entries = pages.items ?? [];
  const atRoot = folder === RAW;
  const parts = folder.split("/");
  const files = entries
    .filter((entry) => entry.kind === "file" && entry.claimed_by === null)
    .map((entry) => entry.name);
  const whole = pages.items === undefined ? null : locationForFolder(folder, files);
  return (
    <section className="raw-browser" aria-label={t("catalog.places.external.browseLabel")}>
      <nav aria-label={t("catalog.places.external.browseTrail")}>
        <ol className="raw-trail">
          {parts.map((part, index) =>
            index === parts.length - 1 ? (
              <li key={index}>
                <span aria-current="location">
                  <VisibleText text={part} />
                </span>
              </li>
            ) : (
              <li key={index}>
                <Button
                  className="link-button"
                  onPress={() => {
                    setFolder(parts.slice(0, index + 1).join("/"));
                  }}
                >
                  <VisibleText text={part} />
                </Button>
              </li>
            ),
          )}
        </ol>
      </nav>
      <p className="raw-hint">
        {atRoot
          ? t("catalog.places.external.browseRootHint")
          : t("catalog.places.external.browseHint")}
      </p>
      <ViewList
        pages={pages}
        rowCount={entries.length}
        empty={t("catalog.places.external.browseEmpty", { path: folder })}
        region={t("catalog.places.external.browseRegion", { path: folder })}
        head={
          <tr>
            <th scope="col">{t("catalog.places.external.browseName")}</th>
            <th scope="col" className="number">
              {t("catalog.places.external.browseSize")}
            </th>
            <th scope="col">{t("catalog.places.external.browseInUse")}</th>
          </tr>
        }
      >
        {entries.map((entry) => (
          <tr key={`${entry.kind}\n${entry.name}`}>
            <td>
              <Button
                className="link-button"
                isDisabled={entry.claimed_by !== null || (entry.kind === "file" && atRoot)}
                onPress={() => {
                  if (entry.kind === "folder") {
                    setFolder(`${folder}/${entry.name}`);
                  } else {
                    onChoose(locationForFile(folder, entry.name));
                  }
                }}
              >
                <VisibleText text={entry.name} />
                {entry.kind === "folder" ? "/" : ""}
              </Button>
            </td>
            <td className="number">
              {entry.size === null ? "" : formatBytes(entry.size, i18n.language)}
            </td>
            <td className="raw-in-use">
              <InUse entry={entry} />
            </td>
          </tr>
        ))}
      </ViewList>
      {!atRoot && (
        <Button
          className="button"
          isDisabled={whole === null}
          onPress={() => {
            if (whole !== null) {
              onChoose(whole);
            }
          }}
        >
          {t("catalog.places.external.useFolder")}
        </Button>
      )}
    </section>
  );
}
