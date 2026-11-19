/**
 * An environment's Leftover directories: the table directories older releases left that no
 * table owns, found and the old ones deleted, for those who manage environments on the
 * instance.
 *
 * @remarks
 * It loads on its own, inside a live environment's page. The search asks for no passkey touch;
 * the deletion (Delete them, Continue and its Try again) asks for one first when the last is
 * old, as the server does, since the files cannot be restored. A recent directory is never
 * offered.
 * @packageDocumentation
 */
import { type JSX, useEffect, useState } from "react";
import { Button, Input, Label, NumberField, Text } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { ApiError } from "../api/client";
import {
  deleteOrphans,
  findOrphans,
  type OrphanedLocationOut,
  type OrphanedLocationsOut,
} from "../api/environments";
import { formatBytes } from "../catalog/format";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { PendingButton } from "../shell/PendingButton";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import "./environments.css";
import "./leftoverStrings";

const L = "settings.leftover";

/** How old a directory's newest file must be by default, as the server's default. */
const DEFAULT_OLDER_THAN_DAYS = 30;

/** The most days the server takes. */
const MAX_DAYS = 3650;

/** How long a call waits before the panel says what it waits for. */
const WAIT_WORDS_MS = 1_000;

/** What the panel shows: the last search, or the last deletion's report. */
interface Shown {
  /** Whether it is a search or a deletion. */
  readonly kind: "found" | "deleted";
  /** The server's answer. */
  readonly report: OrphanedLocationsOut;
}

/** A call that failed, with whether it can be tried again. */
interface Failed {
  /** The server's sentence. */
  readonly text: string;
  /** What Try again repeats; null when asking again does not help (502). */
  readonly again: "found" | "deleted" | null;
  /** The days the failed call asked with, which Try again asks with too. */
  readonly days: number;
}

/**
 * The state of one directory in words.
 *
 * @param t - The translator.
 * @param kind - A search or a deletion.
 * @param location - The directory.
 * @returns The words.
 */
function stateOf(
  t: (key: string) => string,
  kind: Shown["kind"],
  location: OrphanedLocationOut,
): string {
  if (kind === "found") return t(location.recent ? `${L}.recent` : `${L}.old`);
  if (location.deleted) return t(`${L}.deleted`);
  return location.kept_because ?? t(location.recent ? `${L}.recent` : `${L}.kept`);
}

/** Properties of {@link LeftoverDirectories}. */
export interface LeftoverDirectoriesProps {
  /** The environment's name. */
  readonly name: string;
}

/**
 * Renders the panel: the age, Find, what was found, and Delete the old ones with its report.
 *
 * @param props - The environment.
 * @param props.name - The environment's name.
 * @returns The panel.
 */
export function LeftoverDirectories({ name }: LeftoverDirectoriesProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const when = useWhen();
  const [days, setDays] = useState(DEFAULT_OLDER_THAN_DAYS);
  const [busy, setBusy] = useState<Shown["kind"] | null>(null);
  const [slowFor, setSlowFor] = useState<Shown["kind"] | null>(null);
  const [shown, setShown] = useState<Shown | null>(null);
  const [failed, setFailed] = useState<Failed | null>(null);
  const [confirming, setConfirming] = useState(false);
  const { target: summary, arm: armSummary } = useFocusAfter<HTMLParagraphElement>(shown);
  useEffect(() => {
    if (busy === null) return;
    const timer = setTimeout(() => {
      setSlowFor(busy);
    }, WAIT_WORDS_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [busy]);
  const valid = Number.isInteger(days) && days >= 1 && days <= MAX_DAYS;
  const size = (bytes: number): string => formatBytes(bytes, i18n.language);
  /**
   * Searches, or deletes the old directories, and shows the answer or why it failed.
   *
   * @param kind - A search or a deletion.
   * @param olderThan - The days: a deletion's are those of the list the person confirmed, never
   *   the field's if it changed since.
   * @returns When the answer is shown.
   */
  const run = async (kind: Shown["kind"], olderThan: number): Promise<void> => {
    setBusy(kind);
    setSlowFor(null);
    setFailed(null);
    try {
      const report = await (kind === "found"
        ? findOrphans(name, olderThan)
        : deleteOrphans(name, olderThan));
      if (kind === "deleted") armSummary();
      setShown({ kind, report });
    } catch (failure: unknown) {
      // A 502 says the storage refused the credentials: asking again does not help.
      const refused = (failure as Partial<ApiError>).status === 502;
      setFailed({ text: problemText(t, failure), again: refused ? null : kind, days: olderThan });
    } finally {
      setBusy(null);
    }
  };
  const locations = shown?.report.locations ?? [];
  const old = locations.filter((location) => !location.recent);
  const oldBytes = old.reduce((sum, location) => sum + location.bytes, 0);
  const total = locations.reduce((sum, location) => sum + location.bytes, 0);
  // What the question names and the deletion asks with: the list as found.
  const foundDays = shown?.report.older_than_days ?? days;
  const shownComplete = shown?.report.complete ?? true;
  return (
    <section className="flat-panel settings-panel">
      <h2>{t(`${L}.title`)}</h2>
      <p className="page-purpose">{t(`${L}.intro`)}</p>
      <div className="leftover-ask">
        <NumberField
          className="field"
          value={days}
          onChange={setDays}
          minValue={1}
          maxValue={MAX_DAYS}
          formatOptions={{ useGrouping: false }}
        >
          <Label>{t(`${L}.olderThan`)}</Label>
          <Input />
          <Text slot="description">{t(`${L}.olderThanHelp`)}</Text>
        </NumberField>
        <PendingButton
          className="button"
          pending={busy === "found"}
          isDisabled={!valid || (busy !== null && busy !== "found")}
          onPress={() => {
            void run("found", days);
          }}
        >
          {shown === null ? t(`${L}.find`) : t(`${L}.findAgain`)}
        </PendingButton>
      </div>
      <p className="step-status" role="status">
        {busy !== null && slowFor === busy
          ? t(busy === "found" ? `${L}.looking` : `${L}.deleting`)
          : ""}
      </p>
      {failed !== null && (
        <div className="step-error" role="alert">
          <p>{failed.text}</p>
          {failed.again !== null && (
            <Button
              className="button"
              onPress={() => {
                void run(failed.again as Shown["kind"], failed.days);
              }}
            >
              {t(`${L}.tryAgain`)}
            </Button>
          )}
        </div>
      )}
      {shown !== null && (
        <>
          <p tabIndex={-1} ref={summary}>
            {locations.length === 0
              ? t(`${L}.nothing`)
              : t(`${L}.summary`, { count: locations.length, size: size(total) })}
          </p>
          {shown.kind === "found" && foundDays !== days && (
            <p>{t(`${L}.stale`, { found: foundDays, days })}</p>
          )}
          {!shown.report.complete && (
            <p>{t(shown.kind === "found" ? `${L}.partial` : `${L}.partialDelete`)}</p>
          )}
          {locations.length > 0 && (
            <div
              className="list-scroll leftover-scroll"
              role="region"
              aria-label={t(`${L}.region`)}
              // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
              // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
              tabIndex={0}
            >
              <table className="list list-static">
                <thead>
                  <tr>
                    <th scope="col">{t(`${L}.location`)}</th>
                    <th scope="col" className="number">
                      {t(`${L}.files`)}
                    </th>
                    <th scope="col" className="number">
                      {t(`${L}.bytes`)}
                    </th>
                    <th scope="col">{t(`${L}.newest`)}</th>
                    <th scope="col">{t(`${L}.state`)}</th>
                  </tr>
                </thead>
                <tbody>
                  {locations.map((location) => (
                    <tr key={location.location}>
                      <td>
                        <span className="row-name leftover-location">
                          <VisibleText text={location.location} />
                        </span>
                      </td>
                      <td className="number">{location.files}</td>
                      <td className="number">{size(location.bytes)}</td>
                      <td>
                        {location.newest_file_at === null
                          ? t(`${L}.unknown`)
                          : when(location.newest_file_at)}
                      </td>
                      <td>{stateOf(t, shown.kind, location)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="step-actions">
            {shown.kind === "found" && old.length > 0 && (
              <PendingButton
                className="button"
                pending={busy === "deleted"}
                isDisabled={busy === "found"}
                onPress={() => {
                  setConfirming(true);
                }}
              >
                {t(`${L}.deleteOld`)}
              </PendingButton>
            )}
            {shown.kind === "deleted" && !shown.report.complete && (
              <PendingButton
                className="button"
                pending={busy === "deleted"}
                onPress={() => {
                  void run("deleted", shown.report.older_than_days);
                }}
              >
                {t(`${L}.continue`)}
              </PendingButton>
            )}
          </div>
        </>
      )}
      {confirming && (
        <ConfirmDialog
          title={t(shownComplete ? `${L}.confirmTitle` : `${L}.confirmTitlePartial`, {
            count: old.length,
            size: size(oldBytes),
          })}
          confirmLabel={t(`${L}.confirm`)}
          onConfirm={() => run("deleted", foundDays)}
          onCancel={() => {
            setConfirming(false);
          }}
          onDone={() => {
            setConfirming(false);
          }}
        >
          <p>{t(`${L}.confirmBody`, { count: foundDays })}</p>
          {!shownComplete && <p>{t(`${L}.confirmPartial`)}</p>}
        </ConfirmDialog>
      )}
    </section>
  );
}
