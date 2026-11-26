/**
 * How Settings › Audit log shows an event's fields: as the server sends them, with what it hid
 * from the reader said in words.
 *
 * @remarks
 * The server decides what is shown (`lakana_core/audit/content.py`): of another principal's
 * event, a reader without `READ_QUERY_TEXT` gets a query's text as its SHA-256 and length and a
 * withheld value as `{"hidden": true}`. Nothing here reveals or guesses more; it only names the
 * two forms.
 * @packageDocumentation
 */
import type { AuditEventOut } from "../api/audit";

/** One field's value, as the page shows it. */
export type DetailValue =
  | { readonly kind: "text"; readonly text: string }
  | { readonly kind: "none" }
  | { readonly kind: "withheld" }
  | { readonly kind: "fingerprint"; readonly sha256: string; readonly length: number };

/** One field of an event's detail. */
export interface DetailField {
  /** The field's name, as the server wrote it. */
  readonly name: string;
  /** Its value. */
  readonly value: DetailValue;
}

/**
 * Tells whether a value is an object with exactly the given keys.
 *
 * @param value - The value.
 * @param keys - The keys, sorted.
 * @returns Whether it is.
 */
function hasExactly(value: unknown, keys: readonly string[]): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  return JSON.stringify(Object.keys(value).sort()) === JSON.stringify(keys);
}

/**
 * Reads one field's value.
 *
 * @param value - The value as the server sent it.
 * @returns The value to show.
 */
export function detailValue(value: unknown): DetailValue {
  if (value === null || value === undefined) return { kind: "none" };
  if (typeof value === "string") return { kind: "text", text: value };
  if (hasExactly(value, ["hidden"]) && value.hidden === true) return { kind: "withheld" };
  if (
    hasExactly(value, ["length", "sha256"]) &&
    typeof value.sha256 === "string" &&
    /^[0-9a-f]{64}$/.test(value.sha256) &&
    typeof value.length === "number"
  ) {
    return { kind: "fingerprint", sha256: value.sha256, length: value.length };
  }
  return { kind: "text", text: JSON.stringify(value) };
}

/**
 * An event's fields, in the server's order.
 *
 * @param event - The event.
 * @returns The fields.
 */
export function detailFields(event: AuditEventOut): readonly DetailField[] {
  return Object.entries(event.detail).map(([name, value]) => ({ name, value: detailValue(value) }));
}

/**
 * The object an event is about, as the parts the On column joins: none, the instance, an
 * environment, a namespace in it, or a table in that.
 *
 * @param event - The event.
 * @returns Null for no object, `instance`, or the environment and the dotted rest.
 */
export function eventObject(
  event: AuditEventOut,
): null | "instance" | { readonly environment: string; readonly rest: string | null } {
  if (event.securable_kind === null) return null;
  if (event.securable_kind === "INSTANCE" || event.securable_environment === null) {
    return "instance";
  }
  const parts = [event.securable_namespace, event.securable_table].filter(
    (part): part is string => part !== null,
  );
  return {
    environment: event.securable_environment,
    rest: parts.length === 0 ? null : parts.join("."),
  };
}
