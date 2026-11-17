import { defaultKeymap } from "@codemirror/commands";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, runScopeHandlers } from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";

import { runKeys } from "./keys";

let view: EditorView | undefined;

afterEach(() => {
  view?.destroy();
});

/**
 * An editor with the run keys above CodeMirror's default keymap, which binds Mod-Enter to a new
 * blank line.
 *
 * @param onRun - Called on Mod-Enter.
 * @param onCancel - Called on Escape.
 * @returns The editor.
 */
function editor(onRun: () => void, onCancel: () => boolean): EditorView {
  view = new EditorView({
    state: EditorState.create({
      doc: "select 1",
      extensions: [keymap.of(defaultKeymap), runKeys(onRun, onCancel)],
    }),
    parent: document.body,
  });
  return view;
}

/**
 * Hands a key press to the editor's keymaps.
 *
 * @param target - The editor.
 * @param init - The key and its modifiers.
 * @returns Whether a binding handled it.
 */
function press(target: EditorView, init: KeyboardEventInit): boolean {
  return runScopeHandlers(target, new KeyboardEvent("keydown", init), "editor");
}

describe("runKeys", () => {
  it("Mod-Enter runs and inserts no line", () => {
    const onRun = vi.fn();
    const target = editor(onRun, () => false);

    expect(press(target, { key: "Enter", ctrlKey: true })).toBe(true);

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(target.state.doc.toString()).toBe("select 1");
  });

  it("Escape calls onCancel and lets it through when nothing was cancelled", () => {
    const onCancel = vi.fn(() => false);
    const target = editor(() => undefined, onCancel);

    expect(press(target, { key: "Escape" })).toBe(false);
    expect(onCancel).toHaveBeenCalledTimes(1);

    onCancel.mockReturnValue(true);
    expect(press(target, { key: "Escape" })).toBe(true);
    expect(onCancel).toHaveBeenCalledTimes(2);
  });
});
