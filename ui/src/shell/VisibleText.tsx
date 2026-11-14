/**
 * Untrusted text, shown as text nodes only, with every invisible or control character made
 * visible (docs/agent/security.md, Preview and Defined in).
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";

/**
 * The characters shown as a mark instead of themselves: C0 controls except tab and line feed,
 * DEL and C1, the line and paragraph separators, and every format character (`Cf`), which holds
 * the bidi controls U+202A to U+202E and U+2066 to U+2069 and the zero-width characters. It is
 * the set the CLI escapes. Use it with `matchAll` or `replace`, which do not keep `lastIndex`.
 */
// Matching control characters is the point of this pattern.
// eslint-disable-next-line no-control-regex
export const INVISIBLE = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u2028\u2029\p{Cf}]/gu;

/** Properties of {@link VisibleText}. */
export interface VisibleTextProps {
  /** The text, as the server sent it. */
  readonly text: string;
  /**
   * Offer a line break after `_`, `.`, `,`, `/` and `(`, so a long identifier in a narrow cell
   * breaks between its parts rather than inside a word; off by default.
   */
  readonly breakable?: boolean | undefined;
}

/** After each of these characters a breakable text offers a line break. */
const BREAK_AFTER = /(?<=[_.,/(])/;

/**
 * A string with a `<wbr>` after each character of {@link BREAK_AFTER}.
 *
 * @param text - A plain part of the text.
 * @param key - The part's place, for the keys of its pieces.
 * @returns The pieces and the breaks between them.
 */
function withBreaks(text: string, key: number): ReactNode[] {
  return text
    .split(BREAK_AFTER)
    .flatMap((piece, index) =>
      index === 0 ? [piece] : [<wbr key={`${String(key)}-${String(index)}`} />, piece],
    );
}

/**
 * The mark's words for a character: `U+` and its code point, at least four hex digits.
 *
 * @param character - The character.
 * @returns The mark, such as `U+202E`.
 */
function markOf(character: string): string {
  // A match is never empty, so it has a first code point.
  const code = character.codePointAt(0) as number;
  return `U+${code.toString(16).toUpperCase().padStart(4, "0")}`;
}

/** A part of a text: plain text, or the mark that stands for one character or a run of it. */
export type MarkedPart = string | { readonly mark: string };

/**
 * Splits text into plain parts and marks: every character of {@link INVISIBLE} becomes a mark
 * such as `U+202E`, and a run of the same character one mark with its count, such as
 * `U+200B ×350000`, so a file full of one invisible character cannot freeze the page with a
 * mark per character. Tabs and line feeds stay in the plain parts. {@link VisibleText} and
 * {@link VisibleTspan} both show text through it, so HTML and SVG mark the same characters.
 *
 * @param text - The text, as the server sent it.
 * @returns The parts, in order; none for an empty text.
 */
export function markInvisible(text: string): readonly MarkedPart[] {
  // Plain text, or a run of one marked character and how many it holds.
  const parts: (string | { character: string; count: number })[] = [];
  let start = 0;
  for (const match of text.matchAll(INVISIBLE)) {
    const last = parts.at(-1);
    if (match.index === start && typeof last === "object" && last.character === match[0]) {
      last.count += 1;
    } else {
      if (match.index > start) {
        parts.push(text.slice(start, match.index));
      }
      parts.push({ character: match[0], count: 1 });
    }
    start = match.index + match[0].length;
  }
  if (start < text.length) {
    parts.push(text.slice(start));
  }
  return parts.map((part) =>
    typeof part === "string"
      ? part
      : {
          mark:
            part.count === 1
              ? markOf(part.character)
              : `${markOf(part.character)} ×${String(part.count)}`,
        },
  );
}

/**
 * The text as one string with every invisible or control character replaced by its mark, for a
 * place that takes only plain text, such as an SVG `<title>`. Built on {@link markInvisible}, so
 * it marks the same characters.
 *
 * @param text - The text, as the server sent it.
 * @returns The text with marks such as `U+202E` in place of those characters.
 */
export function markedText(text: string): string {
  return markInvisible(text)
    .map((part) => (typeof part === "string" ? part : part.mark))
    .join("");
}

/**
 * Renders text as text nodes, never as markup, and every character of {@link INVISIBLE} as a
 * visible mark such as `U+202E` (see {@link markInvisible}), so a file, a path or a value cannot
 * hide or reorder what the person reads.
 *
 * @param props - The text.
 * @param props.text - The text, as the server sent it.
 * @param props.breakable - Whether to offer line breaks between the parts of an identifier.
 * @returns The text and its marks.
 */
export function VisibleText({ text, breakable = false }: VisibleTextProps): JSX.Element {
  const shown: ReactNode[] = markInvisible(text).flatMap((part, index) =>
    typeof part === "string" ? (
      breakable ? (
        withBreaks(part, index)
      ) : (
        part
      )
    ) : (
      <span key={index} className="char-mark">
        {part.mark}
      </span>
    ),
  );
  return <>{shown}</>;
}

/**
 * {@link VisibleText} inside an SVG `<text>`: the same parts, each mark a
 * `<tspan class="char-mark">`, so a name in a drawing carries the same marks as in HTML.
 *
 * @param props - The text.
 * @param props.text - The text, as the server sent it.
 * @returns The text and its marks, as SVG text content.
 */
export function VisibleTspan({ text }: Omit<VisibleTextProps, "breakable">): JSX.Element {
  const shown: ReactNode[] = markInvisible(text).map((part, index) =>
    typeof part === "string" ? (
      part
    ) : (
      <tspan key={index} className="char-mark">
        {part.mark}
      </tspan>
    ),
  );
  return <>{shown}</>;
}
