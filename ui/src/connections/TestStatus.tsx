/**
 * A connection's last test as a glyph and its words, with the time it was done.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { ConnectionOut } from "../api";
import { StatusGlyph } from "../status/StatusGlyph";
import { testStatus } from "./testStatus";

/** Properties of {@link TestStatus}. */
export interface TestStatusProps {
  /** The connection. */
  readonly connection: ConnectionOut;
  /** The present, in milliseconds since the epoch. */
  readonly now: number;
  /** When the screen first saw the test queued, for "no worker yet". */
  readonly queuedSince?: number;
}

/**
 * Renders the glyph and the words of the last test, the time in a `<time>` element. The glyph
 * and the word always go together; a test never run or older than the connection's last change
 * has no glyph and is muted.
 *
 * @param props - The connection, the present and when it was first seen queued.
 * @param props.connection - The connection.
 * @param props.now - The present.
 * @param props.queuedSince - When the test was first seen queued.
 * @returns The status.
 */
export function TestStatus({ connection, now, queuedSince }: TestStatusProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const view = testStatus(connection, now, i18n.language, t, queuedSince);
  const muted = view.kind === "never" || view.kind === "stale";
  return (
    <span className={muted ? "test-status test-status-muted" : "test-status"}>
      {view.glyph !== null && <StatusGlyph kind={view.glyph} />}
      <span>
        {view.lead}
        {view.when !== null && (
          <time dateTime={connection.last_test?.finished_at ?? connection.updated_at}>
            {view.when}
          </time>
        )}
        {view.trail}
      </span>
    </span>
  );
}
