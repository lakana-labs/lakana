/**
 * The work of the Queries screen: the library of saved and recent queries, the toolbar (the
 * query's name, Save…, Delete…, Format, the environment and Run, which becomes Cancel while the
 * run is active), the SQL editor, what went wrong, and the result.
 *
 * @packageDocumentation
 */
import {
  type JSX,
  lazy,
  type ReactNode,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { listQueryEnvironments, type QueryEnvironmentOut } from "../api/queries";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { Loaded } from "../shell/Loaded";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { shortcutLabel } from "../shell/shortcut";
import { useFolded } from "../shell/useFolded";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import type { StatusKind } from "../status/status";
import { StatusLabel } from "../status/StatusLabel";
import { DeleteSavedDialog } from "./DeleteSavedDialog";
import { catalogCache } from "./editor/catalog";
import type { SqlEditorHandle } from "./editor/SqlEditor";
import { Library } from "./Library";
import { LibraryFold } from "./LibraryFold";
import { savedPath } from "./paths";
import { endedMessage, QueryError } from "./QueryError";
import { QueryNote } from "./QueryNote";
import { readEnvironment, readFilter, writeEnvironment } from "./remember";
import { ResultPanel } from "./ResultPanel";
import { savedWho } from "./SavedList";
import { SaveQueryDialog } from "./SaveQueryDialog";
import { useAfter } from "./useAfter";
import { type QueriesAddress, type QueriesState, useOpenedSaved } from "./useOpenedSaved";
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
  /** The run or the saved query the address names; null on a new query. */
  readonly address: QueriesAddress | null;
  /** The Queries ▾ button of a narrow screen, first in the toolbar; null beside the library. */
  readonly fold: ReactNode;
  /**
   * Called after a save or a delete, a submit taken or a followed run's end, so the library reads
   * its lists again; the same function on every render.
   */
  readonly onLibraryChange: () => void;
}

/**
 * The toolbar, the editor and the run, once the environments are known. An address that names
 * no run (a new query, a saved one) shows no run: entering it stops following the old one
 * (without a cancel) and keeps the editor's text.
 *
 * @param props - The environments, the address, the fold and the library's reload.
 * @param props.environments - The environments where the person may run a query.
 * @param props.address - The run or saved query the address names, or null.
 * @param props.fold - The Queries ▾ button, or null.
 * @param props.onLibraryChange - Called when the library's lists changed.
 * @returns The work column.
 */
function Work({ environments, address, fold, onLibraryChange }: WorkProps): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const navigate = useNavigate();
  const location = useLocation();
  const state = location.state as QueriesState | null;
  const query = useQueryRun();
  const saved = useOpenedSaved(address);
  const { opened, error: savedError } = saved;
  const editor = useRef<SqlEditorHandle | null>(null);
  const runButton = useRef<HTMLButtonElement>(null);
  const [sql, setSql] = useState("");
  // Why the last Format changed nothing: it could not read the text, or its code could not
  // load; null when it worked. The next edit takes the line away.
  const [formatLine, setFormatLine] = useState<"unreadable" | "unloaded" | null>(null);
  const [remembered, setRemembered] = useState(readEnvironment);
  const [dialog, setDialog] = useState<"save" | "delete" | null>(null);
  // A choice holds for the address it was made at; another address brings its own.
  const at = address === null ? "" : `${address.kind}/${address.environment}/${address.id}`;
  const [picked, setPicked] = useState<{ at: string; environment: string } | null>(null);
  const fallback = environments.some((listed) => listed.name === remembered)
    ? (remembered as string)
    : (environments[0] as QueryEnvironmentOut).name;
  const environment =
    picked !== null && picked.at === at ? picked.environment : (address?.environment ?? fallback);
  // An opened run or saved query may live where the person may no longer run one; it shows,
  // but no run is sent there until a listed environment is chosen.
  const allowed = environments.some((listed) => listed.name === environment);

  // A run or a saved query opened from its address brings its SQL into the editor once per
  // visit of the address; a run submitted here leaves the editor as the person left it. A new
  // query reads the remembered environment again, which a run elsewhere may have changed.
  // (State adjusted while rendering, as React documents for state that follows a change.)
  const [seen, setSeen] = useState(at);
  const [loaded, setLoaded] = useState<string | null>(null);
  if (seen !== at) {
    setSeen(at);
    setLoaded(null);
    if (address === null) {
      setRemembered(readEnvironment());
    }
  }
  if (query.opened && query.run !== null && query.run.query_id !== loaded) {
    setLoaded(query.run.query_id);
    if (query.run.sql !== null) {
      setSql(query.run.sql);
    }
  }
  if (address?.kind === "saved" && opened !== null && opened.id !== loaded) {
    setLoaded(opened.id);
    setSql(opened.sql ?? "");
  }

  // Recent lists a run once its submit is taken, and its new state once it ends.
  const { listed } = query;
  useEffect(() => {
    if (listed > 0) {
      onLibraryChange();
    }
  }, [listed, onLibraryChange]);

  const { open, reset } = query;
  const runAddress = address?.kind === "run" ? address : null;
  const runEnvironment = runAddress?.environment;
  const runId = runAddress?.id;
  useEffect(() => {
    if (runEnvironment !== undefined && runId !== undefined) {
      open(runEnvironment, runId);
    } else {
      reset();
    }
  }, [open, reset, runEnvironment, runId]);

  // Nothing to run until the saved query is read, and nothing when its SQL may not be read.
  const readOnly =
    (address?.kind === "saved" && opened === null) || (opened !== null && opened.sql === undefined);
  const active = query.phase === "waiting";
  const run = (): void => {
    // Ctrl/⌘ Enter in the editor while a run is submitted or active starts no second one.
    if (active || query.phase === "submitting" || readOnly || !allowed) {
      return;
    }
    writeEnvironment(environment);
    void query.submit(environment, sql, { saved: opened });
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
  // A saved query's address names it once it is read; until then the toolbar names nothing.
  let name = t("queries.untitled");
  if (opened !== null) {
    name = opened.name;
  } else if (runAddress !== null) {
    name = t("queries.runName", { id: runAddress.id.slice(0, 8) });
  } else if (address !== null) {
    name = "";
  }
  const close = (): void => {
    setDialog(null);
  };
  return (
    <div className="queries-work">
      <div className="queries-toolbar">
        {fold}
        <p className="queries-name">
          <VisibleText text={name} />
        </p>
        <Button
          className="button"
          isDisabled={readOnly}
          onPress={() => {
            setDialog("save");
          }}
        >
          {t("queries.save.open")}
        </Button>
        {opened?.actions.includes("delete") === true && (
          <Button
            className="button"
            onPress={() => {
              setDialog("delete");
            }}
          >
            {t("queries.delete.open")}
          </Button>
        )}
        <Button
          className="button"
          isDisabled={readOnly}
          onPress={() => {
            void editor.current?.format().then(
              (formatted) => {
                setFormatLine(formatted ? null : "unreadable");
              },
              () => {
                setFormatLine("unloaded");
              },
            );
          }}
        >
          {t("queries.format")}
        </Button>
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
          isDisabled={readOnly || (!active && !allowed)}
          onPress={active ? query.cancel : run}
        >
          {active
            ? t(query.cancelling ? "queries.cancelling" : "queries.cancel")
            : t("queries.run")}
        </PendingButton>
        {/* Beside Run, not in it: text inside a button belongs in its name (WCAG 2.5.3). */}
        {!active && (
          <kbd className="queries-run-keys" aria-hidden="true">
            {t(`queries.runKeys.${shortcutLabel(navigator)}`)}
          </kbd>
        )}
      </div>
      {!allowed && (
        <p className="queries-saved-facts">{t("queries.notAllowed", { environment })}</p>
      )}
      {state?.note !== undefined && <QueryNote note={state.note} />}
      {savedError !== null && (
        // The server's cause, then the next step: Try again when another read may help, and a
        // new query in every case.
        <div role="alert" className="step-error queries-alert">
          <p>{savedError}</p>
          {saved.retryable && (
            <Button className="button" onPress={saved.reload}>
              {t("queries.tryAgain")}
            </Button>
          )}
          <Link to="/queries">{t("queries.newQuery")}</Link>
        </div>
      )}
      {opened !== null && opened.sql === undefined && (
        <>
          <p className="queries-saved-facts">
            {savedWho(t, opened, me.name)} <EnvironmentLabel environment={opened.environment} />
          </p>
          <p className="queries-saved-facts">{t("queries.notReadable")}</p>
        </>
      )}
      <div className="queries-editor">
        <Suspense fallback={<EditorSkeleton />}>
          <SqlEditor
            value={sql}
            onChange={(changed) => {
              setSql(changed);
              setFormatLine(null);
            }}
            onRun={run}
            onCancel={query.cancel}
            readOnly={readOnly}
            catalog={catalogCache(environment)}
            error={mark}
            label={t("queries.sql")}
            handle={(handle) => {
              editor.current = handle;
            }}
          />
        </Suspense>
      </div>
      {formatLine !== null && (
        <p role="alert" className="step-error queries-alert">
          {t(formatLine === "unreadable" ? "queries.formatUnreadable" : "queries.formatUnloaded")}
        </p>
      )}
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
          saved={opened?.name ?? null}
          onMore={query.more}
        />
      )}
      {dialog === "save" && (
        <SaveQueryDialog
          environment={environment}
          sql={sql}
          opened={opened}
          onSaved={(saved, created) => {
            close();
            onLibraryChange();
            // A new saved query moves to its address; one saved again stays where it is (its
            // own address, or the run's), and its history entry carries it as it is now.
            void navigate(created ? savedPath(saved.environment, saved.id) : location.pathname, {
              replace: !created,
              state: { keepFocus: true, saved },
            });
          }}
          onCancel={close}
        />
      )}
      {dialog === "delete" && opened !== null && (
        <DeleteSavedDialog
          saved={opened}
          onDone={() => {
            close();
            onLibraryChange();
            // The SQL stays in the editor; the note takes the focus from the gone Delete….
            void navigate("/queries", {
              state: { keepFocus: true, note: { kind: "deleted", name: opened.name } },
            });
          }}
          onCancel={close}
        />
      )}
    </div>
  );
}

/** Properties of {@link QueryWork}. */
export interface QueryWorkProps {
  /**
   * What the address names: a new query, a run (`/queries/{environment}/runs/{id}`) or a saved
   * query (`/queries/{environment}/saved/{id}`).
   */
  readonly mode: "new" | "run" | "saved";
}

/**
 * The Queries screen's work: reads the environments where the person may run a query; with
 * none, says how to get the right, and the command line meanwhile. With some, the library
 * (Saved and Recent) stands beside the work, or folds behind the Queries ▾ button below 640 px.
 *
 * @param props - What the address names.
 * @param props.mode - A new query, a run or a saved query.
 * @returns The page.
 */
export function QueryWork({ mode }: QueryWorkProps): JSX.Element {
  const { t } = useTranslation();
  const params = useParams();
  const me = useMe();
  const folded = useFolded();
  const environments = useResource(listQueryEnvironments);
  const [filter, setFilter] = useState(readFilter);
  const [reloadKey, setReloadKey] = useState(0);
  const reloadLibrary = useCallback(() => {
    setReloadKey((key) => key + 1);
  }, []);
  const address =
    mode === "new" ? null : { kind: mode, environment: String(params.env), id: String(params.id) };
  return (
    <div className="queries-page">
      <Page title={t("areas.queries.name")}>
        <div className="queries">
          <Loaded resource={environments}>
            {(listed) => {
              if (listed.length === 0) {
                return (
                  <div className="queries-work">
                    <p>{t("queries.noEnvironment")}</p>
                    <p>{t("queries.noEnvironmentCli")}</p>
                    <CopyLine text={CLI_QUERY} copyLabel={t("queries.copy")} />
                  </div>
                );
              }
              const library = {
                me: me.name,
                environments: listed,
                filter,
                onFilter: setFilter,
                reloadKey,
              };
              return (
                <>
                  {!folded && (
                    <aside className="queries-library" aria-label={t("queries.library.label")}>
                      <Library {...library} />
                    </aside>
                  )}
                  <Work
                    environments={listed}
                    address={address}
                    fold={folded ? <LibraryFold {...library} /> : null}
                    onLibraryChange={reloadLibrary}
                  />
                </>
              );
            }}
          </Loaded>
        </div>
      </Page>
    </div>
  );
}
