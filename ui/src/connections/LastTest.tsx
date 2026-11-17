/**
 * A connection's last test on its page: the status with its full date, why a failure happened
 * and what to do about it, and the command that runs the same test.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { ConnectionOut } from "../api";
import { CopyLine } from "../shell/CopyLine";
import { shellQuote } from "../shell/shellQuote";
import { useWhen } from "../shell/useWhen";
import { markedText, VisibleText } from "../shell/VisibleText";
import { StatusGlyph } from "../status/StatusGlyph";
import { OUTCOMES } from "./kinds";
import { testStatus } from "./testStatus";
import { NO_WORKER_AFTER_MS } from "./useOpenTests";
import "./connectionStrings";

/** Properties of {@link LastTest}. */
export interface LastTestProps {
  /** The connection. */
  readonly connection: ConnectionOut;
  /** The present, in milliseconds since the epoch. */
  readonly now: number;
  /** When the screen first saw the test queued, for "no worker yet". */
  readonly queuedSince?: number | undefined;
  /** Whether the test is open for so long that it is no longer followed. */
  readonly stuck?: boolean;
}

/**
 * Renders the last test: the glyph and the words with the full date, never only a relative one.
 *
 * @remarks
 * A failure says its cause and the next step, which everyone who reads the connection sees; a
 * manager also gets the server's message and who ran it. A test older than the connection's last
 * change says to test again. A test queued for long says no worker has taken it, and one open
 * for ten minutes says Test again closes it. The command that tests it from a terminal is shown
 * only to people who may test it.
 * @param props - The connection, the present and what the screen saw of the test.
 * @param props.connection - The connection.
 * @param props.now - The present.
 * @param props.queuedSince - When the test was first seen queued.
 * @param props.stuck - Whether the test is no longer followed.
 * @returns The panel.
 */
export function LastTest({
  connection,
  now,
  queuedSince,
  stuck = false,
}: LastTestProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const when = useWhen();
  const view = testStatus(connection, now, i18n.language, t, queuedSince);
  const last = connection.last_test;
  const finished = last?.finished_at ?? connection.updated_at;
  // An outcome this build does not know is worded as "failed", as the status is.
  const outcome =
    last?.outcome != null && OUTCOMES.includes(last.outcome) ? last.outcome : "failed";
  const message = last?.message;
  const who = last?.requested_by;
  const muted = view.kind === "never" || view.kind === "stale";
  return (
    <section className="flat-panel" aria-labelledby="connection-last-test">
      <h2 className="connection-heading" id="connection-last-test">
        {t("connections.page.lastTestHeading")}
      </h2>
      <p className={muted ? "test-status test-status-muted" : "test-status"}>
        {view.glyph !== null && <StatusGlyph kind={view.glyph} />}
        <span>
          {view.lead}
          {view.when !== null && <time dateTime={finished}>{when(finished)}</time>}
          {view.trail}
        </span>
      </p>
      {view.kind === "stale" && <p>{t("connections.page.stale")}</p>}
      {view.kind === "failed" && (
        <>
          <p>{t(`connections.outcomes.${outcome}.cause`)}</p>
          <p>{t(`connections.outcomes.${outcome}.next`)}</p>
        </>
      )}
      {typeof message === "string" && message !== "" && (
        <p className="connection-message">
          <VisibleText text={message} />
        </p>
      )}
      {typeof who === "string" && (
        <p className="second">{t("connections.page.by", { who: markedText(who) })}</p>
      )}
      {view.kind === "queued" &&
        queuedSince !== undefined &&
        now - queuedSince >= NO_WORKER_AFTER_MS && <p>{t("connections.page.noWorker")}</p>}
      {stuck && <p>{t("connections.follow.stuck")}</p>}
      {connection.actions.includes("test") && (
        <CopyLine
          text={`lakana connection test ${shellQuote(connection.name)} --env ${shellQuote(connection.environment)}`}
          copyLabel={t("connections.list.copy")}
        />
      )}
    </section>
  );
}
