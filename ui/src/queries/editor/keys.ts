/**
 * The editor's run and cancel keys.
 *
 * @packageDocumentation
 */
import { type Extension, Prec } from "@codemirror/state";
import { keymap } from "@codemirror/view";

/**
 * Ctrl/⌘ Enter runs the query, and Escape cancels a running one.
 *
 * @remarks
 * Mod-Enter is bound at the highest precedence, above the default keymap's new blank line, so it
 * runs and inserts nothing. Escape is bound low, so the completion list's, a tooltip's and
 * CodeMirror's own Escape run first; it stops there only when `onCancel` cancelled something,
 * and otherwise passes on.
 * @param onRun - Runs the query.
 * @param onCancel - Cancels the running query; true when it cancelled one.
 * @returns The extension.
 */
export function runKeys(onRun: () => void, onCancel: () => boolean): Extension {
  return [
    Prec.highest(
      keymap.of([
        {
          key: "Mod-Enter",
          run: () => {
            onRun();
            return true;
          },
        },
      ]),
    ),
    Prec.low(keymap.of([{ key: "Escape", run: () => onCancel() }])),
  ];
}
