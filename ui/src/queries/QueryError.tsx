/**
 * What went wrong with a run, above the result: a refused submit in the server's words, a run
 * that failed or was cancelled with its message (and Go to its line and column), or a run whose
 * state could not be read, with Try again.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import type { JSX } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { QueryRunOut } from "../api/queries";
import { VisibleText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import type { QueryRunState } from "./useQueryRun";
import "./queryStrings";

/**
 * Why a run that failed or was cancelled ended: the server's message, else a sentence of ours.
 *
 * @param run - The run.
 * @param t - The translation function.
 * @returns The sentence.
 */
export function endedMessage(run: QueryRunOut, t: TFunction): string {
  return (
    run.error_message ?? (run.status === "FAILED" ? t("queries.failed") : t("queries.cancelled"))
  );
}

/** Properties of {@link QueryError}. */
export interface QueryErrorProps {
  /** The run's state. */
  readonly state: QueryRunState;
  /** Moves the editor's cursor to a line and a column, both from 1, and focuses it. */
  readonly onGoTo: (line: number, column: number) => void;
  /** Follows the run again. */
  readonly onRetry: () => void;
}

/**
 * Renders the alert for a run's state; nothing while there is nothing wrong.
 *
 * @param props - The state and the two actions.
 * @param props.state - The run's state.
 * @param props.onGoTo - Moves the editor's cursor to the error.
 * @param props.onRetry - Follows the run again.
 * @returns The alert, or nothing.
 */
export function QueryError({ state, onGoTo, onRetry }: QueryErrorProps): JSX.Element | null {
  const { t } = useTranslation();
  const { phase, run, error } = state;
  if (phase === "refused") {
    return (
      <p role="alert" className="step-error queries-alert">
        {problemText(t, error)}
      </p>
    );
  }
  if (phase === "failed") {
    return (
      <div role="alert" className="step-error queries-alert">
        <p>{t("queries.lost", { problem: problemText(t, error) })}</p>
        <Button className="button" onPress={onRetry}>
          {t("queries.tryAgain")}
        </Button>
      </div>
    );
  }
  if (run === null || (run.status !== "FAILED" && run.status !== "CANCELLED")) {
    return null;
  }
  const { error_line: line, error_column: column } = run;
  return (
    <div role="alert" className="step-error queries-alert">
      <p>
        <VisibleText text={endedMessage(run, t)} />
      </p>
      {line !== null && column !== null && (
        <Button
          className="button"
          onPress={() => {
            onGoTo(line, column);
          }}
        >
          {t("queries.goTo", { line, column })}
        </Button>
      )}
    </div>
  );
}
