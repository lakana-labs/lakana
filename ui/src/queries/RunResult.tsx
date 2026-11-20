/**
 * What the Queries screen shows of a run under the editor: why a cancel was refused, the run's
 * state, what went wrong, and the result.
 *
 * @packageDocumentation
 */
import type { JSX, RefObject } from "react";
import { useTranslation } from "react-i18next";

import { problemText } from "../sign-in/messages";
import type { SqlEditorHandle } from "./editor/SqlEditor";
import { QueryError } from "./QueryError";
import { ResultPanel } from "./ResultPanel";
import { RunLine } from "./RunLine";
import type { QueryRun } from "./useQueryRun";
import "./queryStrings";

/** Properties of {@link RunResult}. */
export interface RunResultProps {
  /** The run. */
  readonly query: QueryRun;
  /** The editor, where Go to puts the cursor. */
  readonly editor: RefObject<SqlEditorHandle | null>;
  /** Run's button, where Try again puts the focus. */
  readonly runButton: RefObject<HTMLButtonElement | null>;
  /** The environment the result is read from. */
  readonly environment: string;
  /** The query's name, which the result's grid is named after. */
  readonly name: string;
  /** The saved query's name, for the downloads' file names; null for one not saved. */
  readonly saved: string | null;
}

/**
 * Renders the cancel's refusal, the run line, the run's alert and, once the run succeeded, the
 * result.
 *
 * @param props - The run and where its actions lead.
 * @param props.query - The run.
 * @param props.editor - The editor.
 * @param props.runButton - Run's button.
 * @param props.environment - The result's environment.
 * @param props.name - The query's name.
 * @param props.saved - The saved query's name, or null.
 * @returns The run's part of the page.
 */
export function RunResult({
  query,
  editor,
  runButton,
  environment,
  name,
  saved,
}: RunResultProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      {query.cancelRefusal !== null && (
        <p role="alert" className="step-error queries-alert">
          {t("queries.cancelRefused", { problem: problemText(t, query.cancelRefusal) })}
        </p>
      )}
      <RunLine state={query} />
      <QueryError
        state={query}
        onGoTo={(line, column) => {
          editor.current?.focusAt(line, column);
        }}
        onRetry={() => {
          // Try again goes with the alert it is in: the focus moves to Run, which is Cancel
          // while the run is followed again.
          query.retry();
          runButton.current?.focus();
        }}
      />
      {query.phase === "done" && query.run?.status === "SUCCEEDED" && (
        <ResultPanel
          environment={environment}
          run={query.run}
          page={query.page}
          name={name}
          saved={saved}
          onMore={query.more}
        />
      )}
    </>
  );
}
