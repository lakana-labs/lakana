/**
 * The column under the pointer, explained: its name, its type and its description.
 *
 * @packageDocumentation
 */
import type { Extension } from "@codemirror/state";
import { type HoverTooltipSource, hoverTooltip } from "@codemirror/view";

import type { ColumnOut } from "../../api/table";
import { markedName, markedText } from "../../shell/VisibleText";
import type { CompletionCatalog } from "./catalog";
import { columnsRead, tableOf } from "./completion";
import { statementAt } from "./fromClause";

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
  dom.append(line(`${markedName(column.name)} · ${markedText(column.type)}`, "cm-column-name"));
  if (column.description !== null) {
    dom.append(line(markedText(column.description), "cm-column-description"));
  }
  return dom;
}

/**
 * The source of the hover: over the column of `namespace.table.column` or `alias.column`, or a
 * column's name alone that exactly one table the statement reads has, the column explained.
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
    const statement = statementAt(view.state.doc, pos);
    let column: ColumnOut | undefined;
    if (qualifier.length === 0) {
      // A word before a dot names a table or a namespace.
      if (view.state.sliceDoc(pos + after.length, pos + after.length + 1) === ".") {
        return null;
      }
      const having = (await columnsRead(catalog, statement)).flatMap(({ columns }) =>
        columns.filter((each) => each.name === name),
      );
      // A name that several tables have is ambiguous: which one is meant is not known.
      column = having.length === 1 ? having[0] : undefined;
    } else {
      const namespaces = await catalog.namespaces();
      const target = tableOf(qualifier, namespaces, statement);
      const columns =
        target === null ? null : await catalog.columns(target.namespace, target.table);
      column = columns?.find((each) => each.name === name);
    }
    if (column === undefined) {
      return null;
    }
    const found = column;
    return {
      pos: pos + after.length - name.length,
      end: pos + after.length,
      above: true,
      create: () => ({ dom: explained(found) }),
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
