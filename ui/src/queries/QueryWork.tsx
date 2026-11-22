/**
 * The work of the Queries screen: the library of saved and recent queries beside the editor bar
 * ({@link QueryToolbar}), the saved query's facts, the SQL editor, and the run under it
 * ({@link RunResult}). New query (and its keys, {@link isNewQueryKey}) clears them all.
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
  useEffectEvent,
  useRef,
  useState,
} from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { listQueryEnvironments, type QueryEnvironmentOut } from "../api/queries";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { Loaded } from "../shell/Loaded";
import { useMe } from "../shell/me";
import { overlayOpen } from "../shell/overlay";
import { Page } from "../shell/Page";
import { useFolded } from "../shell/useFolded";
import { useResource } from "../shell/useResource";
import { markedText } from "../shell/VisibleText";
import { DeleteSavedDialog } from "./DeleteSavedDialog";
import { catalogCache } from "./editor/catalog";
import type { SqlEditorHandle } from "./editor/SqlEditor";
import { Library } from "./Library";
import { LibraryFold } from "./LibraryFold";
import { NewQueryDialog } from "./NewQueryDialog";
import { isNewQueryKey } from "./newQueryKeys";
import { savedPath } from "./paths";
import { endedMessage } from "./QueryError";
import { QueryNote, type QueryNoteState } from "./QueryNote";
import { QueryToolbar } from "./QueryToolbar";
import { readEnvironment, readFilter, writeEnvironment } from "./remember";
import { RunResult } from "./RunResult";
import { savedWho } from "./SavedList";
import { SaveQueryDialog } from "./SaveQueryDialog";
import { useAfter } from "./useAfter";
import { type QueriesAddress, useOpenedSaved } from "./useOpenedSaved";
import { useQueryNote } from "./useQueryNote";
import { useQueryRun } from "./useQueryRun";
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

/** Properties of {@link Work}. */
interface WorkProps {
  /** The environments where the person may run a query; at least one. */
  readonly environments: readonly QueryEnvironmentOut[];
  /** The run or the saved query the address names; null on a new query. */
  readonly address: QueriesAddress | null;
  /** The Queries ▾ button of a narrow screen, first in the toolbar; null beside the library. */
  readonly fold: ReactNode;
  /** Called after a save or a delete, so Saved is read again; the same function on every render. */
  readonly onSavedChange: () => void;
  /**
   * Called after a submit taken or a followed run's end, so Recent is read again; the same
   * function on every render.
   */
  readonly onRecentChange: () => void;
}

/**
 * The toolbar, the editor and the run, once the environments are known. An address that names
 * no run (a new query, a saved one) shows no run: entering it stops following the old one
 * (without a cancel) and keeps the editor's text. New query (the button, or Alt Shift N anywhere
 * on the screen but in a dialog, a menu or a popover) and a delete clear the editor too; New
 * query asks first when the editor holds text that is neither empty nor kept (the opened saved
 * query's, or the run's at a run's address). A run that is active goes on, as when the page is
 * left: Recent lists it.
 *
 * @param props - The environments, the address, the fold and the library's reloads.
 * @param props.environments - The environments where the person may run a query.
 * @param props.address - The run or saved query the address names, or null.
 * @param props.fold - The Queries ▾ button, or null.
 * @param props.onSavedChange - Called when Saved changed.
 * @param props.onRecentChange - Called when Recent changed.
 * @returns The work column.
 */
function Work({
  environments,
  address,
  fold,
  onSavedChange,
  onRecentChange,
}: WorkProps): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const navigate = useNavigate();
  const location = useLocation();
  const query = useQueryRun();
  const saved = useOpenedSaved(address);
  const note = useQueryNote();
  const { opened, error: savedError } = saved;
  const editor = useRef<SqlEditorHandle | null>(null);
  const runButton = useRef<HTMLButtonElement>(null);
  // The address whose saved query Try again is reading again: once it is read, the alert (and
  // Try again with it) goes, and the focus moves to Run, as the run's own Try again does.
  const focusRunAt = useRef<string | null>(null);
  const [sql, setSql] = useState("");
  // Why the last Format changed nothing: it could not read the text, or its code could not
  // load; null when it worked. The next edit takes the line away.
  const [formatLine, setFormatLine] = useState<"unreadable" | "unloaded" | null>(null);
  const [remembered, setRemembered] = useState(readEnvironment);
  const [dialog, setDialog] = useState<"save" | "delete" | "new" | null>(null);
  // Raised to move the focus into the editor after the commit that cleared it, once a dialog that
  // closed with it is gone (React Aria gives the focus back only while nothing else holds it).
  const [focusEditor, setFocusEditor] = useState(0);
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

  useEffect(() => {
    if (opened !== null && focusRunAt.current === at) {
      focusRunAt.current = null;
      runButton.current?.focus();
    }
  }, [opened, at]);
  // A read again that fails keeps the alert, and the focus on its Try again: nothing to move.
  const { failure: savedFailure } = saved;
  useEffect(() => {
    if (savedFailure !== null) {
      focusRunAt.current = null;
    }
  }, [savedFailure]);

  useEffect(() => {
    if (focusEditor > 0) {
      editor.current?.focus();
    }
  }, [focusEditor]);

  // Recent lists a run once its submit is taken, and its new state once it ends.
  const { listed } = query;
  useEffect(() => {
    if (listed > 0) {
      onRecentChange();
    }
  }, [listed, onRecentChange]);

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
  // Text the person would lose: neither empty nor what a saved query or the run keeps.
  const kept = [opened?.sql, runAddress === null ? undefined : query.run?.sql];
  const unsaved = sql.trim() !== "" && !kept.includes(sql);
  // A new, empty query at `/queries`: a push from another address, so Back opens it again; a
  // replace at `/queries` itself. The note is said in the status line; the focus goes to the
  // editor.
  const startNew = (note: QueryNoteState): void => {
    close();
    setSql("");
    setFormatLine(null);
    query.reset();
    setFocusEditor((count) => count + 1);
    void navigate("/queries", { replace: address === null, state: { keepFocus: true, note } });
  };
  // Whether the question was asked from the editor: React Aria cannot give the focus back into
  // the editor's shadow root, so Cancel does.
  const askedInEditor = useRef(false);
  const askNew = (): void => {
    if (unsaved) {
      askedInEditor.current = document.activeElement?.closest(".queries-editor") != null;
      setDialog("new");
    } else {
      startNew({ kind: "new" });
    }
  };
  const onNewKey = useEffectEvent(askNew);
  useEffect(() => {
    const pressed = (event: KeyboardEvent): void => {
      // Before the editor sees it, so the key types nothing there; never behind an overlay.
      if (isNewQueryKey(event) && !overlayOpen()) {
        event.preventDefault();
        onNewKey();
      }
    };
    window.addEventListener("keydown", pressed, { capture: true });
    return () => {
      window.removeEventListener("keydown", pressed, { capture: true });
    };
  }, []);
  return (
    <div className="queries-work">
      <QueryToolbar
        fold={fold}
        name={name}
        readOnly={readOnly}
        deletable={opened?.actions.includes("delete") === true}
        environments={environments}
        environment={environment}
        allowed={allowed}
        query={query}
        runButton={runButton}
        onNew={askNew}
        onSave={() => {
          setDialog("save");
        }}
        onDelete={() => {
          setDialog("delete");
        }}
        onFormat={() => {
          void editor.current?.format().then(
            (formatted) => {
              setFormatLine(formatted ? null : "unreadable");
            },
            () => {
              setFormatLine("unloaded");
            },
          );
        }}
        onEnvironment={(chosen) => {
          setPicked({ at, environment: chosen });
          writeEnvironment(chosen);
        }}
        onRun={run}
      />
      {!allowed && (
        <p className="queries-saved-facts">{t("queries.notAllowed", { environment })}</p>
      )}
      <QueryNote note={note} />
      {savedError !== null && (
        // The server's cause, then the next step: Try again when another read may help, and a
        // new query in every case.
        <div role="alert" className="step-error queries-alert">
          <p>{savedError}</p>
          {saved.retryable && (
            <Button
              className="button"
              onPress={() => {
                focusRunAt.current = at;
                saved.reload();
              }}
            >
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
      <RunResult
        query={query}
        editor={editor}
        runButton={runButton}
        environment={runEnvironment ?? environment}
        name={name}
        saved={opened?.name ?? null}
      />
      {dialog === "save" && (
        <SaveQueryDialog
          environment={environment}
          sql={sql}
          opened={opened}
          onSaved={(saved, created) => {
            close();
            onSavedChange();
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
            onSavedChange();
            // A new, empty query; the focus goes from the gone Delete… to the editor.
            startNew({ kind: "deleted", name: opened.name });
          }}
          onCancel={close}
        />
      )}
      {dialog === "new" && (
        <NewQueryDialog
          onConfirm={() => {
            startNew({ kind: "new" });
          }}
          onCancel={() => {
            close();
            if (askedInEditor.current) {
              setFocusEditor((count) => count + 1);
            }
          }}
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
  // Each list is read again on its own, so one's reload keeps the other's pages (Show more).
  const [savedKey, setSavedKey] = useState(0);
  const [recentKey, setRecentKey] = useState(0);
  const reloadSaved = useCallback(() => {
    setSavedKey((key) => key + 1);
  }, []);
  const reloadRecent = useCallback(() => {
    setRecentKey((key) => key + 1);
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
                savedKey,
                recentKey,
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
                    onSavedChange={reloadSaved}
                    onRecentChange={reloadRecent}
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
