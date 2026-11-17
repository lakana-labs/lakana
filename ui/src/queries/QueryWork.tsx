/**
 * The work of the Queries screen: the toolbar (the query's name, the environment and Run, which
 * becomes Cancel while the run is active), the SQL editor, what went wrong, and the result.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router";

import { listQueryEnvironments, type QueryEnvironmentOut } from "../api/queries";
import { CopyLine } from "../shell/CopyLine";
import { Loaded } from "../shell/Loaded";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { shortcutLabel } from "../shell/shortcut";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import type { StatusKind } from "../status/status";
import { StatusLabel } from "../status/StatusLabel";
import type { SqlEditorHandle } from "./editor/SqlEditor";
import { EnvironmentPicker } from "./EnvironmentPicker";
import { endedMessage, QueryError } from "./QueryError";
import { readEnvironment, writeEnvironment } from "./remember";
import { ResultPanel } from "./ResultPanel";
import { useAfter } from "./useAfter";
import { type QueryRunState, useQueryRun } from "./useQueryRun";
import "./queries.css";
import "./queryStrings";

// CodeMirror loads in a chunk of its own, only when the screen opens.
const SqlEditor = lazy(() => import("./editor/SqlEditor"));

/** The command line that runs a query, for a person who may not run one here yet. */
const CLI_QUERY = 'lakana sql "select 1" --env <environment>';

/**
 * The place of the editor while its chunk loads: empty at first, then, after a second, words.
 *
 * @returns The placeholder.
 */
export function EditorSkeleton(): JSX.Element {
  const { t } = useTranslation();
  const slow = useAfter();
  return (
    <div className="queries-editor-skeleton" role="status">
      {slow && t("queries.loadingEditor")}
    </div>
  );
}

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

/** Properties of {@link Work}. */
interface WorkProps {
  /** The environments where the person may run a query; at least one. */
  readonly environments: readonly QueryEnvironmentOut[];
  /** The run the address names; null on a new query. */
  readonly address: { readonly environment: string; readonly id: string } | null;
}

/**
 * The toolbar, the editor and the run, once the environments are known.
 *
 * @param props - The environments and the run the address names.
 * @param props.environments - The environments where the person may run a query.
 * @param props.address - The run the address names, or null.
 * @returns The work column.
 */
function Work({ environments, address }: WorkProps): JSX.Element {
  const { t } = useTranslation();
  const query = useQueryRun();
  const editor = useRef<SqlEditorHandle | null>(null);
  const runButton = useRef<HTMLButtonElement>(null);
  const [sql, setSql] = useState("");
  const [remembered] = useState(readEnvironment);
  // A choice holds for the address it was made at; another run's address brings its own.
  const at = address === null ? "" : `${address.environment}/${address.id}`;
  const [picked, setPicked] = useState<{ at: string; environment: string } | null>(null);
  const fallback = environments.some((listed) => listed.name === remembered)
    ? (remembered as string)
    : (environments[0] as QueryEnvironmentOut).name;
  const environment =
    picked !== null && picked.at === at ? picked.environment : (address?.environment ?? fallback);

  const { open } = query;
  const runEnvironment = address?.environment;
  const runId = address?.id;
  useEffect(() => {
    if (runEnvironment !== undefined && runId !== undefined) {
      open(runEnvironment, runId);
    }
  }, [open, runEnvironment, runId]);

  // A run opened from its address brings its SQL into the editor once; one submitted here
  // leaves the editor as the person left it. (State adjusted while rendering, as React
  // documents for state that follows a change.)
  const [loaded, setLoaded] = useState<string | null>(null);
  if (query.opened && query.run !== null && query.run.query_id !== loaded) {
    setLoaded(query.run.query_id);
    if (query.run.sql !== null) {
      setSql(query.run.sql);
    }
  }

  const active = query.phase === "waiting";
  const run = (): void => {
    // Ctrl/⌘ Enter in the editor while a run is submitted or active starts no second one.
    if (active || query.phase === "submitting") {
      return;
    }
    writeEnvironment(environment);
    void query.submit(environment, sql);
  };
  const ended = query.run;
  const mark =
    ended !== null && ended.error_line !== null && ended.error_column !== null
      ? {
          line: ended.error_line,
          column: ended.error_column,
          message: markedText(endedMessage(ended, t)),
        }
      : null;
  const name =
    address === null ? t("queries.untitled") : t("queries.runName", { id: address.id.slice(0, 8) });
  return (
    <div className="queries-work">
      <div className="queries-toolbar">
        <p className="queries-name">
          <VisibleText text={name} />
        </p>
        <EnvironmentPicker
          environments={environments}
          value={environment}
          onChange={(chosen) => {
            setPicked({ at, environment: chosen });
            writeEnvironment(chosen);
          }}
        />
        {/* One button that is Run, or Cancel while the run is active, so the focus stays on it. */}
        <PendingButton
          ref={runButton}
          className={active ? "button queries-run" : "button button-primary queries-run"}
          pending={query.phase === "submitting" || query.cancelling}
          onPress={active ? query.cancel : run}
        >
          {active ? (
            t(query.cancelling ? "queries.cancelling" : "queries.cancel")
          ) : (
            <>
              {t("queries.run")}
              <kbd aria-hidden="true">{t(`queries.runKeys.${shortcutLabel(navigator)}`)}</kbd>
            </>
          )}
        </PendingButton>
      </div>
      <div className="queries-editor">
        <Suspense fallback={<EditorSkeleton />}>
          <SqlEditor
            value={sql}
            onChange={setSql}
            onRun={run}
            onCancel={query.cancel}
            readOnly={false}
            environment={environment}
            error={mark}
            label={t("queries.sql")}
            handle={(handle) => {
              editor.current = handle;
            }}
          />
        </Suspense>
      </div>
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
          environment={runEnvironment ?? environment}
          run={query.run}
          page={query.page}
          name={name}
          saved={null}
          onMore={query.more}
        />
      )}
    </div>
  );
}

/** Properties of {@link QueryWork}. */
export interface QueryWorkProps {
  /** What the address names: a new query, or a run (`/queries/{environment}/runs/{id}`). */
  readonly mode: "new" | "run";
}

/**
 * The Queries screen's work: reads the environments where the person may run a query; with
 * none, says how to get the right, and the command line meanwhile.
 *
 * @param props - What the address names.
 * @param props.mode - A new query, or a run.
 * @returns The page.
 */
export function QueryWork({ mode }: QueryWorkProps): JSX.Element {
  const { t } = useTranslation();
  const params = useParams();
  const environments = useResource(listQueryEnvironments);
  const address =
    mode === "run" ? { environment: String(params.env), id: String(params.id) } : null;
  return (
    <Page title={t("areas.queries.name")}>
      <div className="queries">
        <Loaded resource={environments}>
          {(listed) =>
            listed.length === 0 ? (
              <div className="queries-work">
                <p>{t("queries.noEnvironment")}</p>
                <p>{t("queries.noEnvironmentCli")}</p>
                <CopyLine text={CLI_QUERY} copyLabel={t("queries.copy")} />
              </div>
            ) : (
              <Work environments={listed} address={address} />
            )
          }
        </Loaded>
      </div>
    </Page>
  );
}
