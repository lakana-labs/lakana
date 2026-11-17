import { defaultKeymap } from "@codemirror/commands";
import { EditorState } from "@codemirror/state";
import {
  closeHoverTooltips,
  EditorView,
  hasHoverTooltips,
  keymap,
  runScopeHandlers,
} from "@codemirror/view";
import { afterEach, describe, expect, it, vi } from "vitest";

// A hover tooltip opens only on a real mouse over laid-out text, which jsdom has none of; the
// test says when one is open, and the module keeps its own behaviour otherwise.
vi.mock("@codemirror/view", async (original) => {
  const real = await original<typeof import("@codemirror/view")>();
  return { ...real, hasHoverTooltips: vi.fn(real.hasHoverTooltips) };
});

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

  it("Escape closes an open hover tooltip first and cancels only on the next press", () => {
    const onCancel = vi.fn(() => true);
    const target = editor(() => undefined, onCancel);
    const dispatch = vi.spyOn(target, "dispatch");
    vi.mocked(hasHoverTooltips).mockReturnValueOnce(true);

    expect(press(target, { key: "Escape" })).toBe(true);
    expect(onCancel).not.toHaveBeenCalled();
    expect(dispatch).toHaveBeenCalledWith({ effects: closeHoverTooltips });

    expect(press(target, { key: "Escape" })).toBe(true);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(dispatch).toHaveBeenCalledTimes(1);
  });
});
