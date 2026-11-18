/**
 * A task's log as text, followed while the task runs, with Save log once it is complete.
 *
 * @packageDocumentation
 */
import { Fragment, type JSX, memo, useLayoutEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";

import { ReadFailed } from "../catalog/ReadFailed";
import { CopyLine } from "../shell/CopyLine";
import { fileName, saveBlob } from "../shell/saveFile";
import { shellQuote } from "../shell/shellQuote";
import { VisibleText } from "../shell/VisibleText";
import "./jobStrings";
import type { TaskLog } from "./useTaskLog";

/**
 * How many lines one part of the drawn text holds. The parts start at the text's start, so new
 * text changes only the last part and adds new ones; React draws again only those.
 */
const LINES_PER_PART = 200;

/** How close to the end, in pixels, the reader counts as at the end. */
const AT_END_PX = 4;

/**
 * Splits a text into parts of {@link LINES_PER_PART} lines, each with its line ends.
 *
 * @param text - The text.
 * @returns The parts, in order.
 */
function partsOf(text: string): string[] {
  const parts: string[] = [];
  let start = 0;
  let lines = 0;
  for (let end = text.indexOf("\n"); end !== -1; end = text.indexOf("\n", end + 1)) {
    lines += 1;
    if (lines === LINES_PER_PART) {
      parts.push(text.slice(start, end + 1));
      start = end + 1;
      lines = 0;
    }
  }
  if (start < text.length) {
    parts.push(text.slice(start));
  }
  return parts;
}

/** Properties of {@link LogPart}. */
interface LogPartProps {
  /** Some whole lines of the log, the last perhaps without its end. */
  readonly text: string;
}

/**
 * Some lines of the log, each through {@link VisibleText}. Memoised by its text, so a part that
 * did not change is not drawn again when the log grows.
 */
const LogPart = memo(function LogPart({ text }: LogPartProps): JSX.Element {
  const lines = text.split("\n");
  return (
    <>
      {lines.map((line, index) => (
        // The lines of a part never move, so their place is their key.
        <Fragment key={index}>
          <VisibleText text={line} />
          {index < lines.length - 1 && "\n"}
        </Fragment>
      ))}
    </>
  );
});

/** Properties of {@link LogView}. */
export interface LogViewProps {
  /** The run's environment. */
  readonly environment: string;
  /** The run's id. */
  readonly runId: string;
  /** The task's asset. */
  readonly asset: string;
  /** The log, from `useTaskLog`. */
  readonly log: TaskLog;
}

/**
 * The log in a `<pre>` region named after the task, wrapped so that a narrow screen never scrolls
 * sideways, every hidden character marked. It is not a live region: a status line says whether
 * it is followed or complete. New text scrolls it to the end only while the reader is there. A
 * log cut at its start, or too long to follow, says so with the command that prints it whole;
 * a complete log that was not cut offers Save log.
 *
 * @param props - The task and its log.
 * @param props.environment - The run's environment.
 * @param props.runId - The run's id.
 * @param props.asset - The task's asset.
 * @param props.log - The log.
 * @returns The log.
 */
export function LogView({ environment, runId, asset, log }: LogViewProps): JSX.Element {
  const { t } = useTranslation();
  const { text, complete, cut, stopped, ended } = log;
  const parts = useMemo(() => partsOf(text), [text]);
  const region = useRef<HTMLPreElement>(null);
  const atEnd = useRef(true);
  useLayoutEffect(() => {
    const pre = region.current;
    if (pre !== null && atEnd.current) {
      pre.scrollTop = pre.scrollHeight;
    }
  }, [text]);
  const command = `lakana runs logs ${shellQuote(runId)} --task ${shellQuote(asset)} --env ${shellQuote(environment)}`;
  const statusLine = (): string => {
    if (complete) {
      return t("jobs.log.complete");
    }
    if (stopped || log.error !== null) {
      return "";
    }
    return t(ended ? "jobs.log.ended" : "jobs.log.following");
  };
  return (
    <>
      <p className="jobs-reading" role="status">
        {statusLine()}
      </p>
      {log.error !== null && <ReadFailed message={log.error} onRetry={log.retry} />}
      {(stopped || cut) && (
        <div className="jobs-log-note">
          <p>{stopped ? t("jobs.log.tooLong") : t("jobs.log.earlier")}</p>
          <CopyLine text={command} copyLabel={t("comingSoon.copy")} />
        </div>
      )}
      {text === "" ? (
        complete && <p>{t("jobs.log.empty")}</p>
      ) : (
        <pre
          ref={region}
          className="jobs-log"
          role="region"
          aria-label={t("jobs.log.region", { asset })}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          onScroll={(event) => {
            const pre = event.currentTarget;
            atEnd.current = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - AT_END_PX;
          }}
        >
          {parts.map((part, index) => (
            // A part keeps its place while the log grows at its end.
            <LogPart key={index} text={part} />
          ))}
        </pre>
      )}
      {complete && !cut && text !== "" && (
        <button
          type="button"
          className="button"
          onClick={() => {
            saveBlob(new Blob([text]), fileName(`${asset}-${runId.slice(0, 8)}.log`));
          }}
        >
          {t("jobs.log.save")}
        </button>
      )}
    </>
  );
}
