/**
 * The column under the pointer, explained: its name, its type and its description.
 *
 * @packageDocumentation
 */
import type { Extension } from "@codemirror/state";
import { type HoverTooltipSource, hoverTooltip } from "@codemirror/view";

import type { ColumnOut } from "../../api/table";
import { markedText } from "../../shell/VisibleText";
import type { CompletionCatalog } from "./catalog";
import { statementAt, tableOf } from "./completion";

// The dotted path that ends at a position, and the rest of the word after it.
const PATH_BEFORE = /(?:[\p{L}\p{N}_$]+\.)*[\p{L}\p{N}_$]*$/u;
const WORD_AFTER = /^[\p{L}\p{N}_$]*/u;

/**
 * A line of the tooltip, as a text node in its own block.
 *
 * @param text - The line's text.
 * @param className - The block's class.
 * @returns The block.
 */
function line(text: string, className: string): HTMLElement {
  const block = document.createElement("div");
  block.className = className;
  block.append(document.createTextNode(text));
  return block;
}

/**
 * The tooltip's content: "{{column}} · {{type}}", then the description if there is one.
 *
 * @param column - The column.
 * @returns The content, text nodes only.
 */
function explained(column: ColumnOut): HTMLElement {
  const dom = document.createElement("div");
  dom.className = "cm-column-hover";
  dom.append(line(`${markedText(column.name)} · ${markedText(column.type)}`, "cm-column-name"));
  if (column.description !== null) {
    dom.append(line(markedText(column.description), "cm-column-description"));
  }
  return dom;
}

/**
 * The source of the hover: over the column of `namespace.table.column` or `alias.column`, the
 * column explained.
 *
 * @param catalog - The environment's catalog.
 * @returns The source; it answers null over anything else.
 */
export function columnTooltip(catalog: CompletionCatalog): HoverTooltipSource {
  return async (view, pos) => {
    const at = view.state.doc.lineAt(pos);
    const before = (PATH_BEFORE.exec(at.text.slice(0, pos - at.from)) as RegExpExecArray)[0];
    const after = (WORD_AFTER.exec(at.text.slice(pos - at.from)) as RegExpExecArray)[0];
    const qualifier = `${before}${after}`.split(".");
    const name = qualifier.pop() as string;
    if (qualifier.length === 0) {
      return null;
    }
    const namespaces = await catalog.namespaces();
    const target = tableOf(qualifier, namespaces, statementAt(view.state.doc, pos));
    const columns = target === null ? null : await catalog.columns(target.namespace, target.table);
    const column = columns?.find((each) => each.name === name);
    if (column === undefined) {
      return null;
    }
    return {
      pos: pos + after.length - name.length,
      end: pos + after.length,
      above: true,
      create: () => ({ dom: explained(column) }),
    };
  };
}

/**
 * The hover of the catalog's columns.
 *
 * @param catalog - The environment's catalog.
 * @returns The extension.
 */
export function columnHover(catalog: CompletionCatalog): Extension {
  return hoverTooltip(columnTooltip(catalog));
}
