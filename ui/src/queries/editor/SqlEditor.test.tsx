import { diagnosticCount, forEachDiagnostic } from "@codemirror/lint";
import { EditorView } from "@codemirror/view";
import { act, render, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import type { CompletionCatalog } from "./catalog";
import { editorSources, registerEditorSource } from "./extensions";
import SqlEditor, { type SqlEditorHandle } from "./SqlEditor";

type Props = ComponentProps<typeof SqlEditor>;

const SQL = "select *\nfrom gold.station_ranking";

/**
 * A catalog that knows no names, as the screen hands one in for an environment.
 *
 * @returns The catalog.
 */
function emptyCatalog(): CompletionCatalog {
  return {
    namespaces: () => Promise.resolve([]),
    tables: () => Promise.resolve([]),
    columns: () => Promise.resolve(null),
  };
}

const PROD = emptyCatalog();

/**
 * The properties of an editor, each callback a mock.
 *
 * @param overrides - What differs.
 * @returns The properties.
 */
function props(overrides: Partial<Props> = {}): Props {
  return {
    value: SQL,
    onChange: vi.fn(),
    onRun: vi.fn(),
    onCancel: vi.fn(() => false),
    readOnly: false,
    catalog: PROD,
    error: null,
    label: "SQL of Untitled query",
    ...overrides,
  };
}

/**
 * The editor's shadow root, where CodeMirror mounts its view and styles.
 *
 * @param container - The rendered container.
 * @returns The shadow root.
 */
function shadowOf(container: HTMLElement): ShadowRoot {
  const host = container.querySelector(".sql-editor");
  const shadow = host?.shadowRoot ?? null;
  if (shadow === null) {
    throw new Error("the editor has no shadow root");
  }
  return shadow;
}

/**
 * The CodeMirror view inside the editor.
 *
 * @param container - The rendered container.
 * @returns The view.
 */
function viewOf(container: HTMLElement): EditorView {
  const dom = shadowOf(container).querySelector<HTMLElement>(".cm-editor");
  const view = dom === null ? null : EditorView.findFromDOM(dom);
  if (view === null) {
    throw new Error("no editor view");
  }
  return view;
}

/**
 * Types text at the end, as a person would.
 *
 * @param view - The view.
 * @param text - What to type.
 */
function type(view: EditorView, text: string): void {
  const end = view.state.doc.length;
  view.dispatch({
    changes: { from: end, insert: text },
    selection: { anchor: end + text.length },
    userEvent: "input.type",
  });
}

/**
 * The texts the editor's diagnostics mark.
 *
 * @param view - The view.
 * @returns Each diagnostic's marked text and message.
 */
function marks(view: EditorView): string[] {
  const found: string[] = [];
  forEachDiagnostic(view.state, (diagnostic, from, to) => {
    found.push(`${view.state.sliceDoc(from, to)}: ${diagnostic.message}`);
  });
  return found;
}

describe("SqlEditor", () => {
  it("renders a textbox named by label inside a shadow root", () => {
    const { container } = render(<SqlEditor {...props()} />);

    const shadow = shadowOf(container);
    const box = within(shadow as unknown as HTMLElement).getByRole("textbox", {
      name: "SQL of Untitled query",
    });
    expect(box).toHaveTextContent("select *");
    expect(container.querySelector("[role=textbox]")).toBeNull();
  });

  it("reports each change", () => {
    const onChange = vi.fn();
    const { container, rerender } = render(<SqlEditor {...props({ onChange })} />);
    const view = viewOf(container);

    type(view, " where");
    expect(onChange).toHaveBeenLastCalledWith(`${SQL} where`);
    rerender(<SqlEditor {...props({ onChange, value: `${SQL} where` })} />);
    type(view, " rank");

    expect(onChange).toHaveBeenLastCalledWith(`${SQL} where rank`);
    expect(onChange).toHaveBeenCalledTimes(2);

    // A new value from the screen replaces the text without being reported back.
    rerender(<SqlEditor {...props({ onChange, value: "select 2" })} />);
    expect(view.state.doc.toString()).toBe("select 2");
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("shows a diagnostic for the error and clears it on the next edit", () => {
    const error = { line: 2, column: 6, message: "Table gold.station_ranking does not exist." };
    const { container, rerender } = render(<SqlEditor {...props({ error })} />);
    const view = viewOf(container);

    expect(marks(view)).toEqual(["gold: Table gold.station_ranking does not exist."]);
    expect(shadowOf(container).querySelector(".cm-lint-marker-error")).not.toBeNull();

    type(view, " ");
    expect(diagnosticCount(view.state)).toBe(0);

    // The same error again is not shown again; a new one is.
    rerender(<SqlEditor {...props({ error: { ...error } })} />);
    expect(diagnosticCount(view.state)).toBe(0);
    rerender(<SqlEditor {...props({ error: { ...error, line: 1, column: 1 } })} />);
    expect(marks(view)).toEqual(["select: Table gold.station_ranking does not exist."]);
    rerender(<SqlEditor {...props({ error: null })} />);
    expect(diagnosticCount(view.state)).toBe(0);
  });

  it("is read-only when asked", () => {
    const { container, rerender } = render(<SqlEditor {...props({ readOnly: true })} />);
    const box = () =>
      within(shadowOf(container) as unknown as HTMLElement).getByRole("textbox", {
        name: "SQL of Untitled query",
      });

    expect(box()).toHaveAttribute("contenteditable", "false");
    expect(box()).toHaveAttribute("aria-readonly", "true");
    // A Tab stop, read-only or not, so the region that scrolls a long line is reached by keyboard.
    expect(box()).toHaveAttribute("tabindex", "0");
    expect(viewOf(container).state.readOnly).toBe(true);

    rerender(<SqlEditor {...props({ readOnly: false, label: "SQL of daily_sales" })} />);
    const editable = within(shadowOf(container) as unknown as HTMLElement).getByRole("textbox", {
      name: "SQL of daily_sales",
    });
    expect(editable).toHaveAttribute("contenteditable", "true");
    expect(editable).not.toHaveAttribute("aria-readonly");
    expect(editable).toHaveAttribute("tabindex", "0");
  });

  it("focusAt puts the cursor at the line and column", () => {
    let api: SqlEditorHandle | undefined;
    const { container } = render(
      <SqlEditor
        {...props({
          handle: (given) => {
            api = given;
          },
        })}
      />,
    );
    const view = viewOf(container);

    api?.focusAt(2, 6);
    expect(view.state.selection.main.head).toBe(view.state.doc.line(2).from + 5);
    expect(view.hasFocus).toBe(true);

    view.contentDOM.blur();
    api?.focus();
    expect(view.hasFocus).toBe(true);
  });

  it("format lays the text out as an edit undo takes back, and leaves text it cannot read", async () => {
    let api: SqlEditorHandle | undefined;
    const onChange = vi.fn();
    const { container } = render(
      <SqlEditor
        {...props({
          onChange,
          handle: (given) => {
            api = given;
          },
        })}
      />,
    );
    const view = viewOf(container);

    await act(async () => {
      expect(await api?.format()).toBe(true);
    });
    expect(view.state.doc.toString()).toBe("SELECT\n  *\nFROM\n  gold.station_ranking");
    expect(onChange).toHaveBeenLastCalledWith("SELECT\n  *\nFROM\n  gold.station_ranking");

    type(view, " where name = 'unclosed");
    const unreadable = view.state.doc.toString();
    await act(async () => {
      expect(await api?.format()).toBe(false);
    });
    expect(view.state.doc.toString()).toBe(unreadable);
  });

  it("completes the chosen environment's names, and none without an environment", () => {
    const catalog = (): string[] =>
      editorSources()
        .map((source) => source.id)
        .filter((id) => id === "catalog");
    const { rerender, unmount } = render(<SqlEditor {...props()} />);
    expect(catalog()).toEqual(["catalog"]);

    rerender(<SqlEditor {...props({ catalog: emptyCatalog() })} />);
    expect(catalog()).toEqual(["catalog"]);
    rerender(<SqlEditor {...props({ catalog: null })} />);
    expect(catalog()).toEqual([]);
    rerender(<SqlEditor {...props({ catalog: PROD })} />);
    unmount();
    expect(catalog()).toEqual([]);
  });

  it("starts without the catalog's help when no environment is chosen", () => {
    const { container } = render(<SqlEditor {...props({ catalog: null })} />);

    expect(viewOf(container).state.doc.toString()).toBe(SQL);
    expect(editorSources().map((source) => source.id)).not.toContain("catalog");
  });

  it("runs on Mod-Enter and cancels on Escape", () => {
    const onRun = vi.fn();
    const onCancel = vi.fn(() => true);
    const { container } = render(<SqlEditor {...props({ onRun, onCancel })} />);
    const content = viewOf(container).contentDOM;

    content.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
    );
    content.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("takes up a source registered while it is open", () => {
    const run = vi.fn(() => true);
    const { container } = render(<SqlEditor {...props()} />);
    let unregister = (): void => undefined;
    act(() => {
      unregister = registerEditorSource({ id: "assistant", commands: [{ key: "Mod-i", run }] });
    });

    // Mod-i is the default keymap's "select parent syntax"; a source's command comes first.
    viewOf(container).contentDOM.dispatchEvent(
      new KeyboardEvent("keydown", { key: "i", ctrlKey: true, bubbles: true }),
    );
    act(unregister);

    expect(run).toHaveBeenCalledTimes(1);
  });
});
