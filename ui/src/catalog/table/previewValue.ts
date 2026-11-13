/**
 * One cell of a preview as text: the server's markers for binary and cut values read in words,
 * and every other value as it is or as JSON.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import "./tableStrings";

/** A cell as the preview shows it. */
export interface PreviewText {
  /** The text to show; empty for a cut value that kept none. */
  readonly text: string;
  /** Whether the text stands for the value rather than being it: null, or binary's size. */
  readonly muted: boolean;
  /** Whether the server cut the value to keep the page within its size. */
  readonly cut: boolean;
}

/**
 * The binary types, whose cells the server sends as `{"binary": <bytes>}`: Arrow's names, and
 * the SQL names, in any case.
 */
const BINARY_TYPE =
  /^(?:binary|large_binary|binary_view|fixed_size_binary|blob|bytea|varbinary)(?![\w])/i;

/** The string types, whose cut cells keep a prefix: `{"cut": true, "text": <prefix>}`. */
const STRING_TYPE = /^(?:string|large_string|string_view|utf8|large_utf8|varchar|text)$/i;

/** A union's cell is its member's value, which may be any object, `{"cut": true}` included. */
const UNION_TYPE = /^(?:sparse_|dense_)?union</i;

/**
 * The one struct type whose value can be `{"cut": true}`: a struct of one boolean field named
 * `cut`. Any other struct, and every map (an array), cannot hold it.
 */
const ONLY_CUT_FIELD = /^struct<cut: bool>$/i;

/**
 * Whether a value is a plain object with exactly these keys.
 *
 * @param value - The value.
 * @param keys - The keys, sorted.
 * @returns True for an object with these keys and no others.
 */
function hasKeys(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const own = Object.keys(value).sort();
  return own.length === keys.length && own.every((key, index) => key === keys[index]);
}

/**
 * The server's cut marker, read only where it cannot be a value: `{"cut": true, "text"}` in a
 * string column, and `{"cut": true}` in any column but a union or a `struct<cut: bool>`, and in
 * every column of a row the page lists as cut.
 *
 * @param value - The cell.
 * @param columnType - The column's type name.
 * @param rowCut - Whether the page lists the cell's row as cut.
 * @returns The kept prefix ("" for none), or null when the cell is not a cut marker.
 */
function cutText(value: unknown, columnType: string, rowCut: boolean): string | null {
  if (hasKeys(value, ["cut", "text"])) {
    return value["cut"] === true &&
      typeof value["text"] === "string" &&
      STRING_TYPE.test(columnType)
      ? value["text"]
      : null;
  }
  if (hasKeys(value, ["cut"]) && value["cut"] === true) {
    const mayBeValue = UNION_TYPE.test(columnType) || ONLY_CUT_FIELD.test(columnType);
    return rowCut || !mayBeValue ? "" : null;
  }
  return null;
}

/**
 * Writes one cell as text. A marker counts only with its exact keys and only in the columns
 * where the server writes it, so a real struct with fields named `binary` or `cut` reads as JSON.
 *
 * @param value - The cell as the JSON page has it; undefined for a cell the row does not have.
 * @param columnType - The column's type name.
 * @param rowCut - Whether the page lists the cell's row in `row_cut`.
 * @param t - The translation function.
 * @returns The text, and whether it is muted or cut.
 */
export function previewValue(
  value: unknown,
  columnType: string,
  rowCut: boolean,
  t: TFunction,
): PreviewText {
  if (value === undefined && rowCut) {
    // The page budget left the rest of a cut row out.
    return { text: "", muted: false, cut: true };
  }
  if (value === null || value === undefined) {
    return { text: t("table.preview.null"), muted: true, cut: false };
  }
  if (
    hasKeys(value, ["binary"]) &&
    typeof value["binary"] === "number" &&
    BINARY_TYPE.test(columnType)
  ) {
    return { text: t("table.preview.binary", { count: value["binary"] }), muted: true, cut: false };
  }
  const kept = cutText(value, columnType, rowCut);
  if (kept !== null) {
    return { text: kept, muted: false, cut: true };
  }
  return {
    text: typeof value === "string" ? value : JSON.stringify(value),
    muted: false,
    cut: false,
  };
}
