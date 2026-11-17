/**
 * The editor in Still Water: its theme and its syntax highlighting, from the tokens only.
 *
 * @remarks
 * The editor lives in an open shadow root, which the custom properties of `tokens.css` reach by
 * inheritance, so every value here is a `var(--…)`. CodeMirror's lint marks draw with `data:`
 * images, which the content security policy's `img-src` refuses; the theme replaces every one of
 * them with plain CSS (a wavy underline and a filled dot), so no image is asked for.
 * @packageDocumentation
 */
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import type { Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { tags } from "@lezer/highlight";

/** The editor's surfaces, text, selection, cursor, gutter, focus and floating parts. */
export const stillWaterTheme: Extension = EditorView.theme({
  // The editor fills its resizable host, so a click below short SQL lands in it.
  "&": {
    flex: "1 1 auto",
    minHeight: "0",
    color: "var(--color-text)",
    backgroundColor: "var(--color-surface)",
    fontSize: "var(--type-code-size)",
  },
  "&.cm-focused": {
    outline: "var(--focus-ring)",
    outlineOffset: "var(--focus-offset)",
  },
  ".cm-scroller": {
    fontFamily: "var(--font-mono)",
    lineHeight: "var(--type-code-line)",
  },
  ".cm-content": {
    caretColor: "var(--color-accent)",
    padding: "var(--space-2) 0",
  },
  ".cm-line": {
    padding: "0 var(--space-2)",
  },
  ".cm-cursor, .cm-dropCursor": {
    borderLeftColor: "var(--color-accent)",
  },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    {
      backgroundColor: "var(--color-accent-bg)",
    },
  ".cm-gutters": {
    backgroundColor: "var(--color-surface)",
    color: "var(--color-text-faint)",
    borderRight: "1px solid var(--color-line)",
  },
  ".cm-tooltip": {
    backgroundColor: "var(--color-float)",
    color: "var(--color-text)",
    border: "1px solid var(--color-edge)",
    borderRadius: "var(--radius-control)",
    boxShadow: "var(--shadow-float)",
  },
  ".cm-tooltip-autocomplete > ul": {
    fontFamily: "var(--font-mono)",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    backgroundColor: "var(--color-accent-bg)",
    color: "var(--color-text)",
  },
  ".cm-completionDetail, .cm-completionInfo": {
    color: "var(--color-text-muted)",
  },
  // A column explained under the pointer: its name and type in the code face, then its
  // description.
  ".cm-column-hover": {
    maxWidth: "24rem",
    padding: "var(--space-2) var(--space-3)",
  },
  ".cm-column-name": {
    fontFamily: "var(--font-mono)",
  },
  ".cm-column-description": {
    marginTop: "var(--space-1)",
    color: "var(--color-text-muted)",
    overflowWrap: "anywhere",
  },
  ".cm-diagnostic-error": {
    borderLeftColor: "var(--color-bad)",
  },
  // Every severity's image is replaced, though the editor marks only errors, so a lint upgrade
  // that draws another one fails theme.test.ts rather than the policy.
  ".cm-lintRange-error, .cm-lintRange-warning, .cm-lintRange-info, .cm-lintRange-hint": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--color-bad)",
    textDecorationSkipInk: "none",
  },
  ".cm-lint-marker-error, .cm-lint-marker-warning, .cm-lint-marker-info": {
    content: "none",
    backgroundColor: "var(--color-bad)",
    borderRadius: "var(--radius-round)",
    width: "var(--space-2)",
    height: "var(--space-2)",
    margin: "var(--space-1) auto",
  },
  // A mark at a point (past the end of a line) is a small triangle under it.
  ".cm-lintPoint:after": {
    borderBottomColor: "var(--color-bad)",
  },
  // Forced colours drop backgrounds; the dot keeps the system's text colour.
  "@media (forced-colors: active)": {
    ".cm-lint-marker-error": {
      forcedColorAdjust: "none",
      backgroundColor: "CanvasText",
    },
  },
});

/**
 * The highlighting of SQL, muted: keywords in the text colour at weight 500, strings and numbers
 * muted, comments faint and in italic. No second accent.
 */
export const stillWaterHighlight: Extension = syntaxHighlighting(
  HighlightStyle.define([
    { tag: [tags.keyword, tags.operatorKeyword], color: "var(--color-text)", fontWeight: "500" },
    {
      tag: [tags.string, tags.special(tags.string), tags.number, tags.bool, tags.null],
      color: "var(--color-text-muted)",
    },
    { tag: tags.comment, color: "var(--color-text-faint)", fontStyle: "italic" },
  ]),
);
