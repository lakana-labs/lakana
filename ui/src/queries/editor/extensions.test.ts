import { autocompletion, currentCompletions, startCompletion } from "@codemirror/autocomplete";
import { EditorSelection, EditorState } from "@codemirror/state";
import { EditorView, runScopeHandlers } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  editorSources,
  type EditorSource,
  registerEditorSource,
  sourceExtension,
  subscribeEditorSources,
} from "./extensions";

const cleanups: (() => void)[] = [];

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) {
    cleanup();
  }
});

/**
 * Registers a source for one test.
 *
 * @param source - The source.
 */
function register(source: EditorSource): void {
  cleanups.push(registerEditorSource(source));
}

/**
 * An editor with the registered sources, the cursor at the end.
 *
 * @param doc - The text.
 * @returns The editor.
 */
function editor(doc: string): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc,
      selection: EditorSelection.cursor(doc.length),
      extensions: [autocompletion({ activateOnTyping: false }), sourceExtension(editorSources())],
    }),
    parent: document.body,
  });
  cleanups.push(() => {
    view.destroy();
  });
  return view;
}

describe("the editor's sources", () => {
  it("registers and unregisters a source", () => {
    const listener = vi.fn();
    cleanups.push(subscribeEditorSources(listener));
    const source: EditorSource = { id: "catalog" };

    const unregister = registerEditorSource(source);
    expect(editorSources()).toEqual([source]);
    expect(listener).toHaveBeenCalledTimes(1);

    unregister();
    expect(editorSources()).toEqual([]);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("a source's completion is offered", async () => {
    register({
      id: "catalog",
      completion: (context) => {
        const word = context.matchBefore(/\w*/);
        return { from: word?.from ?? context.pos, options: [{ label: "station_ranking" }] };
      },
    });
    const view = editor("select * from gold.sta");

    startCompletion(view);

    await vi.waitFor(() => {
      expect(currentCompletions(view.state).map((option) => option.label)).toContain(
        "station_ranking",
      );
    });
  });

  it("a source's commands run in the editor", () => {
    const run = vi.fn(() => true);
    register({ id: "assistant", commands: [{ key: "Mod-i", run }] });
    const view = editor("select 1");

    const handled = runScopeHandlers(
      view,
      new KeyboardEvent("keydown", { key: "i", ctrlKey: true }),
      "editor",
    );

    expect(handled).toBe(true);
    expect(run).toHaveBeenCalledTimes(1);
  });
});
