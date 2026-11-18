/**
 * What the connection form holds, and the body it sends.
 *
 * @remarks
 * The form's state holds no secret value (its new values stay `{}`): Save reads them from their
 * password fields into the {@link FormValues} it passes here, and they travel only in the body
 * {@link createBody} or {@link updateBody} returns; nothing here logs, stores or names them.
 * @packageDocumentation
 */
import type { ConnectionCreateIn, ConnectionKind, ConnectionOut, ConnectionUpdateIn } from "../api";
import {
  FREE_KEYS_MAX,
  type FieldSpec,
  kindFields,
  movesSecret,
  s3DefaultEndpoint,
  secretKeys,
  takesSecret,
} from "./kinds";

/**
 * The CA certificate: the stored one kept (on Add: none), a new one read from a file, or the
 * stored one removed.
 */
export type CaChoice =
  | { readonly kind: "keep" }
  | { readonly kind: "new"; readonly pem: string }
  | { readonly kind: "remove" };

/**
 * The secret: the bound one kept, new values by key, an existing secret by name, or none.
 */
export type SecretChoice =
  | { readonly way: "keep" }
  | { readonly way: "values"; readonly values: Readonly<Record<string, string>> }
  | { readonly way: "named"; readonly name: string }
  | { readonly way: "none" };

/** What the connection form holds, every field as typed. */
export interface FormValues {
  /** The environment. */
  readonly environment: string;
  /** The kind. */
  readonly kind: ConnectionKind;
  /** The connection's name. */
  readonly name: string;
  /** The address fields by key; an empty one is left out. */
  readonly address: Readonly<Record<string, string>>;
  /** The settings by key, the CA certificate aside; an empty one is left out. */
  readonly settings: Readonly<Record<string, string>>;
  /** The CA certificate. */
  readonly ca: CaChoice;
  /** The secret. */
  readonly secret: SecretChoice;
}

/**
 * An empty form for a kind: no field typed, no CA certificate, and new secret values.
 *
 * @param environment - The environment.
 * @param kind - The kind.
 * @returns The form.
 */
export function emptyForm(environment: string, kind: ConnectionKind): FormValues {
  return {
    environment,
    kind,
    name: "",
    address: {},
    settings: {},
    ca: { kind: "keep" },
    secret: { way: "values", values: {} },
  };
}

/**
 * The new secret values a choice holds.
 *
 * @param secret - The secret as the form holds it.
 * @returns The values typed, by key; none for any other choice.
 */
function typedValues(secret: SecretChoice): Readonly<Record<string, string>> {
  return secret.way === "values" ? secret.values : {};
}

/**
 * A field's value as the server takes it: a port is a number when it is one, else its text,
 * which the server refuses in its own words.
 *
 * @param key - The field's key.
 * @param value - The text typed.
 * @returns The value.
 */
function wireValue(key: string, value: string): string | number {
  return key === "port" && /^\d+$/.test(value) ? Number(value) : value;
}

/**
 * The fields typed into a part, the empty ones and the PEM one left out.
 *
 * @param fields - The part's fields, in the mirror's order.
 * @param typed - What was typed, by key.
 * @returns The part.
 */
function part(
  fields: readonly FieldSpec[],
  typed: Readonly<Record<string, string>>,
): Record<string, string | number> {
  const result: Record<string, string | number> = {};
  for (const { key, pem } of fields) {
    const value = typed[key] ?? "";
    if (pem !== true && value !== "") {
      result[key] = wireValue(key, value);
    }
  }
  return result;
}

/**
 * The secret values to send: only the keys the kind and its settings take (for `azure` one of
 * its two, for `tcp` at most 16 named keys), each with a value.
 *
 * @param form - The form.
 * @param values - The values typed, by key.
 * @returns The values, or null when none is left.
 */
function sentValues(
  form: FormValues,
  values: Readonly<Record<string, string>>,
): Record<string, string> | null {
  const { required, optional, oneOf, free } = secretKeys(form.kind, form.settings);
  const typed = Object.entries(values).filter(([, value]) => value !== "");
  let kept: [string, string][];
  if (free) {
    kept = typed.slice(0, FREE_KEYS_MAX);
  } else {
    // Of the keys where exactly one is needed (`azure`), only the first with a value goes.
    const one = oneOf?.find((key) => Boolean(values[key]));
    const allowed = new Set([...required, ...optional, ...(one === undefined ? [] : [one])]);
    kept = typed.filter(([key]) => allowed.has(key));
  }
  return kept.length === 0 ? null : Object.fromEntries(kept);
}

/**
 * The body that creates the connection the form holds.
 *
 * @remarks
 * Only the kind's fields (for `http`, those of its auth) are sent, and an empty one is left out,
 * so the server uses its default (for `port`, the kind's; for an `s3` `endpoint`, the region's).
 * A new CA certificate goes as `settings.ca_certificate`. The secret goes as `secret_values`
 * for new values and as `secret` for an existing secret's name, and only when the kind and its
 * settings take one.
 * @param form - The form.
 * @returns The body.
 */
export function createBody(form: FormValues): ConnectionCreateIn {
  const fields = kindFields(form.kind, form.settings);
  const settings: Record<string, string | number> = part(fields.settings, form.settings);
  if (form.ca.kind === "new" && fields.settings.some(({ pem }) => pem === true)) {
    settings.ca_certificate = form.ca.pem;
  }
  const body = {
    name: form.name,
    kind: form.kind,
    address: part(fields.address, form.address),
    settings,
  };
  const { secret } = form;
  if (!takesSecret(form.kind, form.settings)) {
    return body;
  }
  if (secret.way === "named") {
    return { ...body, secret: secret.name };
  }
  const values = sentValues(form, typedValues(secret));
  return values === null ? body : { ...body, secret_values: values };
}

/**
 * A stored value as the form's text.
 *
 * @param value - The value the server sent.
 * @returns Its text; empty for none.
 */
function formText(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  return value === null ? "" : JSON.stringify(value);
}

/**
 * The text of each stored value of a part, the PEM one left out.
 *
 * @param stored - The part as the server sent it.
 * @returns The values by key, as text.
 */
function formPart(stored: Readonly<Record<string, unknown>>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(stored)
      .filter(([key]) => key !== "ca_certificate")
      .map(([key, value]) => [key, formText(value)]),
  );
}

/**
 * A stored address as Edit starts from it: an `s3` endpoint that is its region's default counts
 * as not set, so the field starts empty and a new region takes its own default on the server.
 *
 * @param connection - The connection as stored.
 * @returns The address.
 */
function storedAddress(connection: ConnectionOut): Readonly<Record<string, unknown>> {
  const { kind, address } = connection;
  if (kind !== "s3" || address.endpoint !== s3DefaultEndpoint(address.region)) {
    return address;
  }
  return Object.fromEntries(Object.entries(address).filter(([key]) => key !== "endpoint"));
}

/**
 * The Edit form of a stored connection: its fields as stored, its CA certificate and its secret
 * kept, and no secret value.
 *
 * @param connection - The connection.
 * @returns The form.
 */
export function editForm(connection: ConnectionOut): FormValues {
  return {
    environment: connection.environment,
    kind: connection.kind,
    name: connection.name,
    address: formPart(storedAddress(connection)),
    settings: formPart(connection.settings),
    ca: { kind: "keep" },
    secret: connection.secret === null ? { way: "none" } : { way: "keep" },
  };
}

/**
 * The keys of a part that changed, each with its new value: null for one emptied or no longer
 * asked for.
 *
 * @param fields - The part's fields now and as stored (for `http`, of either auth).
 * @param asked - The keys the kind asks for now.
 * @param stored - The part as stored.
 * @param typed - What the form holds, by key.
 * @returns The changes.
 */
function partChanges(
  fields: readonly FieldSpec[],
  asked: ReadonlySet<string>,
  stored: Readonly<Record<string, unknown>>,
  typed: Readonly<Record<string, string>>,
): Record<string, string | number | null> {
  const result: Record<string, string | number | null> = {};
  for (const { key, pem } of fields) {
    const value = typed[key] ?? "";
    const after = asked.has(key) && value !== "" ? wireValue(key, value) : null;
    if (pem !== true && (stored[key] ?? null) !== after) {
      result[key] = after;
    }
  }
  return result;
}

/**
 * The secret's part of an update.
 *
 * @param before - The connection as stored.
 * @param form - The form.
 * @returns The keys to send for the secret, none when it stays as it is.
 */
function secretChanges(before: ConnectionOut, form: FormValues): ConnectionUpdateIn {
  const bound = before.secret !== null;
  const { secret } = form;
  if (!takesSecret(form.kind, form.settings) || secret.way === "none") {
    return bound ? { clear: ["secret"] } : {};
  }
  if (secret.way === "named") {
    return secret.name === before.secret ? {} : { secret: secret.name };
  }
  const values = sentValues(form, typedValues(secret));
  return values === null ? {} : { secret_values: values };
}

/**
 * The body that changes a stored connection to what the form holds.
 *
 * @remarks
 * Only the keys that changed are sent, and an emptied one (or one the kind no longer asks for,
 * such as a setting of another `http` auth) is `null`, which the server removes. The CA
 * certificate goes only when it was replaced (its text) or removed (`null`). The secret goes as
 * `secret_values` for new values, `secret` for another secret's name and `clear: ["secret"]`
 * to stop using one; kept, it sends nothing.
 * @param before - The connection as stored.
 * @param form - The form.
 * @returns The body, or null when nothing changed.
 */
export function updateBody(before: ConnectionOut, form: FormValues): ConnectionUpdateIn | null {
  const now = kindFields(form.kind, form.settings);
  const then = kindFields(form.kind, formPart(before.settings));
  const keys = (fields: readonly FieldSpec[]): Set<string> => new Set(fields.map(({ key }) => key));
  const asked = keys(now.settings);
  const address = partChanges(now.address, keys(now.address), storedAddress(before), form.address);
  const settings: Record<string, string | number | null> = partChanges(
    [...now.settings, ...then.settings.filter(({ key }) => !asked.has(key))],
    asked,
    before.settings,
    form.settings,
  );
  if (now.settings.some(({ pem }) => pem === true) && form.ca.kind !== "keep") {
    settings.ca_certificate = form.ca.kind === "new" ? form.ca.pem : null;
  }
  const body: ConnectionUpdateIn = {
    ...(Object.keys(address).length > 0 && { address }),
    ...(Object.keys(settings).length > 0 && { settings }),
    ...secretChanges(before, form),
  };
  return Object.keys(body).length === 0 ? null : body;
}

/**
 * A part as the server validates it: a key that is absent or `null` takes its default.
 *
 * @param fields - The part's fields.
 * @param part - The part.
 * @returns The part with the defaults filled in.
 */
function withDefaults(
  fields: readonly FieldSpec[],
  part: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const filled: Record<string, unknown> = { ...part };
  for (const { key, default: fallback } of fields) {
    if (fallback !== undefined && (filled[key] === undefined || filled[key] === null)) {
      filled[key] = fallback;
    }
  }
  return filled;
}

/**
 * Whether saving the form could send the connection's bound secret somewhere new, so that the
 * server asks for its values again (or for the right to manage secrets).
 *
 * @remarks
 * Both sides are compared as the server validates them, so an emptied port (sent as `null`) is
 * the kind's default, not a move.
 * @param before - The connection as stored.
 * @param form - The form.
 * @returns True when the change moves the secret.
 */
export function movesWith(before: ConnectionOut, form: FormValues): boolean {
  const body = updateBody(before, form);
  const was = kindFields(form.kind, before.settings);
  const settings = { ...before.settings, ...body?.settings };
  const now = kindFields(form.kind, settings);
  return movesSecret(
    form.kind,
    {
      address: withDefaults(was.address, before.address),
      settings: withDefaults(was.settings, before.settings),
    },
    {
      address: withDefaults(now.address, { ...before.address, ...body?.address }),
      settings: withDefaults(now.settings, settings),
    },
  );
}
