/**
 * The run's state in words, under the editor.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { VisibleText } from "../shell/VisibleText";
import type { StatusKind } from "../status/status";
import { StatusLabel } from "../status/StatusLabel";
import { useAfter } from "./useAfter";
import type { QueryRunState } from "./useQueryRun";
import "./queryStrings";

/**
 * Why a queued run waits, in the server's words, once it has waited a second.
 *
 * @param props - The sentence.
 * @param props.message - The server's sentence.
 * @returns The sentence, or nothing in the first second.
 */
function WaitingFor({ message }: { readonly message: string }): JSX.Element | null {
  const slow = useAfter();
  return slow ? <VisibleText text={message} /> : null;
}

/** Properties of {@link RunLine}. */
interface RunLineProps {
  /** The run's state. */
  readonly state: QueryRunState;
}

/**
 * The run's state in words: submitting, starting, queued (with the server's reason after a
 * second) or running with its glyph, reading the rows, or how it ended when it did not succeed.
 * A run that succeeded says its rows in the result's own line.
 *
 * @param props - The state.
 * @param props.state - The run's state.
 * @returns The line, a live region that is always there.
 */
export function RunLine({ state }: RunLineProps): JSX.Element {
  const { t } = useTranslation();
  const { phase, run } = state;
  let words: JSX.Element | string | null = null;
  if (phase === "submitting") {
    words = t("queries.state.submitting");
  } else if (phase === "loading_page") {
    words = t("queries.state.reading");
  } else if (run === null) {
    words = phase === "waiting" ? t("queries.state.starting") : null;
  } else if (phase === "waiting" || (phase === "done" && run.status !== "SUCCEEDED")) {
    words = (
      <>
        {/* The five states of a run are five of the glyphs' kinds, in lower case. */}
        <StatusLabel kind={run.status.toLowerCase() as StatusKind} />
        {run.status === "QUEUED" && run.waiting_for !== null && (
          <WaitingFor message={run.waiting_for.message} />
        )}
      </>
    );
  }
  return (
    <p role="status" className="queries-run-line">
      {words}
    </p>
  );
}
