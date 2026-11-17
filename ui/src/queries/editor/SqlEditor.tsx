/**
 * The SQL editor: CodeMirror 6 in an open shadow root, in a chunk of its own.
 *
 * @remarks
 * CodeMirror writes its styles at run time. Mounted in an open shadow root, they become a
 * constructed stylesheet (`adoptedStyleSheets`) rather than a `<style>` element, so the content
 * security policy's `style-src` needs no exception (`e2e/editor-csp.spec.ts`,
 * `build/csp.test.ts`). The custom properties of `tokens.css` inherit into the shadow tree, and
 * the theme (`theme.ts`) is written from them.
 * @packageDocumentation
 */
import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { sql } from "@codemirror/lang-sql";
import { type Diagnostic, lintGutter, setDiagnostics } from "@codemirror/lint";
import { Annotation, Compartment, EditorState, type Extension, Prec } from "@codemirror/state";
import { drawSelection, EditorView, keymap, lineNumbers } from "@codemirror/view";
import { type JSX, useEffect, useEffectEvent, useRef, useState, useSyncExternalStore } from "react";

import { catalogCache } from "./catalog";
import { catalogCompletion } from "./completion";
import { clearOnEdit, errorDiagnostic, errorPosition } from "./errorMark";
import {
  editorSources,
  registerEditorSource,
  sourceExtension,
  subscribeEditorSources,
} from "./extensions";
import { columnHover } from "./hover";
import { runKeys } from "./keys";
import { stillWaterHighlight, stillWaterTheme } from "./theme";

/** What the screen may do with a mounted editor. */
export interface SqlEditorHandle {
  /**
   * Puts the cursor at a line and a column, both from 1 (the column in characters, as the
   * server counts it), scrolls it into view and focuses the editor.
   */
  focusAt(line: number, column: number): void;
  /** Focuses the editor. */
  focus(): void;
  /**
   * Formats the text (the formatter loads on the first call) as an edit of its own, which undo
   * takes back; leaves it as it is when the formatter cannot read it.
   *
   * @returns Whether the text could be read and was formatted.
   */
  format(): Promise<boolean>;
}

/** The server's error position and sentence, which the editor marks. */
interface SqlEditorError {
  /** The line, from 1. */
  readonly line: number;
  /** The column, from 1, in characters. */
  readonly column: number;
  /** The server's sentence, shown on the mark. */
  readonly message: string;
}

/** Properties of {@link SqlEditor}. */
export interface SqlEditorProps {
  /** The SQL text; a new value replaces the editor's text without an `onChange`. */
  readonly value: string;
  /** Called with the whole text after each edit. */
  readonly onChange: (sql: string) => void;
  /** Runs the query (Ctrl/⌘ Enter). */
  readonly onRun: () => void;
  /** Cancels the running query (Escape); true when it cancelled, so Escape stops there. */
  readonly onCancel: () => boolean;
  /** Whether the text may not be changed. */
  readonly readOnly: boolean;
  /** The environment the query runs in, for completion; null when none is chosen. */
  readonly environment: string | null;
  /** The server's error to mark, until the next edit; null for none. */
  readonly error: SqlEditorError | null;
  /** The text box's accessible name, such as "SQL of daily_sales". */
  readonly label: string;
  /** Receives the editor's handle once it is mounted; none by default. */
  readonly handle?: (api: SqlEditorHandle) => void;
}

// Marks the transaction that puts a new value from the screen into the editor, which is not
// reported back through onChange.
const fromScreen = Annotation.define<boolean>();

/**
 * The extension that follows the read-only flag and the name.
 *
 * @param readOnly - Whether the text may not be changed.
 * @param label - The accessible name.
 * @returns The extension.
 */
function settings(readOnly: boolean, label: string): Extension {
  return [
    EditorState.readOnly.of(readOnly),
    EditorView.editable.of(!readOnly),
    // A Tab stop in both cases: the content is the only thing in the region that scrolls a long
    // line, and a read-only text is not focusable by itself.
    EditorView.contentAttributes.of(
      readOnly
        ? { "aria-label": label, "aria-readonly": "true", tabindex: "0" }
        : { "aria-label": label, tabindex: "0" },
    ),
  ];
}

/**
 * The hover of an environment's catalog.
 *
 * @param environment - The environment, or null for none.
 * @returns The extension; none without an environment.
 */
function catalogHover(environment: string | null): Extension {
  return environment === null ? [] : columnHover(catalogCache(environment));
}

/**
 * Renders the editor in an open shadow root on its host element.
 *
 * @param props - The text, the callbacks, the read-only flag, the error and the name.
 * @returns The host element.
 */
export default function SqlEditor(props: SqlEditorProps): JSX.Element {
  const { value, readOnly, label, error, environment } = props;
  // The host as state: the view is created once React has attached it.
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const editor = useRef<EditorView | null>(null);
  const [compartments] = useState(() => ({
    settings: new Compartment(),
    sources: new Compartment(),
    catalog: new Compartment(),
  }));
  const sources = useSyncExternalStore(subscribeEditorSources, editorSources);

  const changed = useEffectEvent((sql: string) => {
    props.onChange(sql);
  });
  const run = useEffectEvent(() => {
    props.onRun();
  });
  const cancel = useEffectEvent(() => props.onCancel());

  // The effects that follow the properties come before the one that creates the view: on the
  // first render they find no view, and the view is created from the same properties.
  useEffect(() => {
    const view = editor.current;
    if (view === null || view.state.doc.toString() === value) {
      return;
    }
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: value },
      annotations: fromScreen.of(true),
    });
  }, [value]);

  const line = error?.line;
  const column = error?.column;
  const message = error?.message;
  useEffect(() => {
    const view = editor.current;
    if (view === null) {
      return;
    }
    view.dispatch(setDiagnostics(view.state, marks(view.state, line, column, message)));
  }, [line, column, message]);

  useEffect(() => {
    editor.current?.dispatch({
      effects: compartments.settings.reconfigure(settings(readOnly, label)),
    });
  }, [compartments, readOnly, label]);

  useEffect(() => {
    editor.current?.dispatch({
      effects: compartments.sources.reconfigure(sourceExtension(sources)),
    });
  }, [compartments, sources]);

  // The catalog of the chosen environment completes names, through the sources' registry, and
  // explains a column under the pointer.
  useEffect(() => {
    editor.current?.dispatch({
      effects: compartments.catalog.reconfigure(catalogHover(environment)),
    });
    return environment === null
      ? undefined
      : registerEditorSource({
          id: "catalog",
          completion: catalogCompletion(catalogCache(environment)),
        });
  }, [compartments, environment]);

  const create = useEffectEvent((shadow: ShadowRoot): EditorView => {
    const created = EditorState.create({
      doc: value,
      extensions: [
        lineNumbers(),
        history(),
        drawSelection(),
        sql(),
        autocompletion(),
        lintGutter(),
        clearOnEdit,
        runKeys(run, cancel),
        keymap.of([...completionKeymap, ...historyKeymap, ...defaultKeymap]),
        stillWaterTheme,
        stillWaterHighlight,
        compartments.settings.of(settings(readOnly, label)),
        compartments.catalog.of(catalogHover(environment)),
        // A source's commands come before the default keymap; Ctrl/⌘ Enter stays the run key.
        Prec.high(compartments.sources.of(sourceExtension(sources))),
        EditorView.updateListener.of((update) => {
          if (
            update.docChanged &&
            !update.transactions.some((transaction) => transaction.annotation(fromScreen) === true)
          ) {
            changed(update.state.doc.toString());
          }
        }),
      ],
    });
    const state = created.update(
      setDiagnostics(created, marks(created, error?.line, error?.column, error?.message)),
    ).state;
    const view = new EditorView({ state, root: shadow, parent: shadow });
    props.handle?.({
      focusAt: (at, character) => {
        view.dispatch({
          selection: { anchor: errorPosition(view.state.doc, at, character) },
          scrollIntoView: true,
        });
        view.focus();
      },
      focus: () => {
        view.focus();
      },
      format: async () => {
        const { formatSql } = await import("./formatSql");
        const formatted = await formatSql(view.state.doc.toString());
        if (formatted === null) {
          return false;
        }
        view.dispatch({
          changes: { from: 0, to: view.state.doc.length, insert: formatted },
          userEvent: "input.format",
        });
        return true;
      },
    });
    return view;
  });

  useEffect(() => {
    if (host === null) {
      return undefined;
    }
    const view = create(host.shadowRoot ?? host.attachShadow({ mode: "open" }));
    editor.current = view;
    return () => {
      editor.current = null;
      view.destroy();
    };
  }, [host]);

  return <div className="sql-editor" ref={setHost} />;
}

/**
 * The diagnostics of the error, if there is one.
 *
 * @param state - The editor's state.
 * @param line - The error's line, or undefined for none.
 * @param column - The error's column.
 * @param message - The error's sentence.
 * @returns One diagnostic or none.
 */
function marks(
  state: EditorState,
  line: number | undefined,
  column: number | undefined,
  message: string | undefined,
): Diagnostic[] {
  return line === undefined || column === undefined || message === undefined
    ? []
    : [errorDiagnostic(state.doc, line, column, message)];
}
