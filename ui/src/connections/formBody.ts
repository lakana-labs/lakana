/**
 * What the connection form holds, and the body it sends.
 *
 * @remarks
 * Secret values live only in a {@link FormValues} held by the form's state, and travel only in
 * the body {@link createBody} returns; nothing here logs, stores or names them.
 * @packageDocumentation
 */
import type { ConnectionCreateIn, ConnectionKind } from "../api";
import { FREE_KEYS_MAX, type FieldSpec, kindFields, secretKeys, takesSecret } from "./kinds";

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
export function typedValues(secret: SecretChoice): Readonly<Record<string, string>> {
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
