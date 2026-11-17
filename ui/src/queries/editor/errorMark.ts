/**
 * The mark of the server's error position in the editor, as a lint diagnostic.
 *
 * @packageDocumentation
 */
import { type Diagnostic, diagnosticCount, setDiagnostics } from "@codemirror/lint";
import { EditorState, type Extension, type Text } from "@codemirror/state";

// What a SQL word is made of, for the end of the mark.
const WORD = /^[\p{L}\p{N}_$]$/u;

/**
 * The offset in a line's text of a column counted in code points from 1, clamped to the line.
 *
 * @param text - The line's text.
 * @param column - The column.
 * @returns The offset in UTF-16 units.
 */
function columnOffset(text: string, column: number): number {
  return Array.from(text)
    .slice(0, Math.max(column - 1, 0))
    .join("").length;
}

/**
 * The document offset of a line and a column, both counted from 1, the column in characters
 * (Unicode code points) as the server counts it; clamped to the document.
 *
 * @param doc - The document.
 * @param line - The line.
 * @param column - The column.
 * @returns The offset.
 */
export function errorPosition(doc: Text, line: number, column: number): number {
  const at = doc.line(Math.min(Math.max(line, 1), doc.lines));
  return at.from + columnOffset(at.text, column);
}

/**
 * The diagnostic of the server's error: from its line and column to the end of the word there,
 * or the one character there when no word starts at it. A line past the last marks the last line,
 * and a column past the end of its line marks the line's end.
 *
 * @param doc - The document.
 * @param line - The line, from 1.
 * @param column - The column, from 1, in code points.
 * @param message - The server's sentence.
 * @returns The diagnostic, severity `error`.
 */
export function errorDiagnostic(
  doc: Text,
  line: number,
  column: number,
  message: string,
): Diagnostic {
  const from = errorPosition(doc, line, column);
  const at = doc.lineAt(from);
  // The characters from the position on, each a whole code point.
  const rest = Array.from(at.text.slice(from - at.from));
  const stop = rest.findIndex((char) => !WORD.test(char));
  const word = stop < 0 ? rest : rest.slice(0, stop);
  const marked = word.length > 0 ? word : rest.slice(0, 1);
  const to = from + marked.join("").length;
  return { from, to, severity: "error", message };
}

/**
 * Clears the error's diagnostic on the first edit after it was shown: the mark belongs to the
 * text the server read.
 */
export const clearOnEdit: Extension = EditorState.transactionExtender.of((transaction) =>
  transaction.docChanged && diagnosticCount(transaction.startState) > 0
    ? setDiagnostics(transaction.startState, [])
    : null,
);
