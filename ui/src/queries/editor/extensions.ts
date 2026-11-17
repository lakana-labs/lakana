/**
 * The editor's extension point: sources of completion and commands that other modules register.
 *
 * @remarks
 * This is the seam for later help in the editor, such as an AI module (a non-goal of the Queries
 * screen itself): a module registers an {@link EditorSource}, and every open editor takes it up
 * at once through a `Compartment` (`SqlEditor`), and drops it when it is unregistered. The
 * catalog's completion is registered the same way.
 * @packageDocumentation
 */
import type { CompletionSource } from "@codemirror/autocomplete";
import { EditorState, type Extension } from "@codemirror/state";
import { type KeyBinding, keymap } from "@codemirror/view";

/** A source of help for the SQL editor. */
export interface EditorSource {
  /** Names the source, for debugging and for tests. */
  readonly id: string;
  /** Offers completions beside the SQL keywords; none when undefined. */
  readonly completion?: CompletionSource;
  /** Key bindings that run in the editor; none when undefined. */
  readonly commands?: readonly KeyBinding[];
}

let sources: readonly EditorSource[] = [];
const listeners = new Set<() => void>();

/** Tells every listener that the sources changed. */
function changed(): void {
  for (const listener of listeners) {
    listener();
  }
}

/**
 * Adds a source to every editor, open or opened later.
 *
 * @param source - The source.
 * @returns A function that removes it again.
 */
export function registerEditorSource(source: EditorSource): () => void {
  sources = [...sources, source];
  changed();
  return () => {
    sources = sources.filter((each) => each !== source);
    changed();
  };
}

/**
 * The sources registered now, in the order they came; the same array until one is added or
 * removed, as `useSyncExternalStore` needs.
 *
 * @returns The sources.
 */
export function editorSources(): readonly EditorSource[] {
  return sources;
}

/**
 * Listens for sources being added or removed.
 *
 * @param listener - Called after each change.
 * @returns A function that stops listening.
 */
export function subscribeEditorSources(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * The editor extension of some sources: each completion source beside the language's own (SQL
 * keywords), and each command in the keymap.
 *
 * @param list - The sources.
 * @returns The extension.
 */
export function sourceExtension(list: readonly EditorSource[]): Extension {
  return list.map((source) => [
    source.completion === undefined
      ? []
      : EditorState.languageData.of(() => [{ autocomplete: source.completion }]),
    source.commands === undefined ? [] : keymap.of(source.commands),
  ]);
}
