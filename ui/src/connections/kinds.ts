/**
 * The nine kinds of connection: their fields, the keys of their secret, what moves a secret
 * and the test outcomes.
 *
 * @remarks
 * This is the only reader of `kinds.mirror.json`, the UI's one mirror of
 * `docs/site/reference/connections.md`. `scripts/tests/test_ui_connection_kinds.py` compares
 * that file with `lakana_core.connections.kinds` and with the reference's tables, so a change
 * on the server that the mirror misses fails there. The server stays the judge of every value.
 * @packageDocumentation
 */
import mirror from "./kinds.mirror.json";

import type { ConnectionKind, ConnectionTestOutcome } from "../api";

/** One field of a kind's address or settings. */
export interface FieldSpec {
  /** The key as the server names it. */
  readonly key: string;
  /** Whether the server refuses the part without it. */
  readonly required: boolean;
  /** The value the server uses when the key is absent. */
  readonly default?: string | number;
  /** The only values the server accepts, when it names them. */
  readonly choices?: readonly string[];
  /** Whether the value is PEM text, entered as a file or a block. */
  readonly pem?: boolean;
}

/** An address and settings, as a connection holds them. */
export interface Parts {
  /** Where the connection points. */
  readonly address: Readonly<Record<string, unknown>>;
  /** How it is reached. */
  readonly settings: Readonly<Record<string, unknown>>;
}

interface Mirror {
  readonly order: readonly ConnectionKind[];
  readonly kinds: Readonly<
    Record<
      ConnectionKind,
      {
        readonly address: readonly FieldSpec[];
        readonly settings: readonly FieldSpec[];
        readonly secret: {
          readonly required?: readonly string[];
          readonly optional?: readonly string[];
          readonly byAuth?: boolean;
          readonly oneOf?: readonly string[];
          readonly free?: boolean;
          readonly max?: number;
        } | null;
      }
    >
  >;
  readonly httpAuth: Readonly<Record<string, readonly string[]>>;
  readonly httpAuthRequired: Readonly<Record<string, readonly string[]>>;
  readonly httpSecretKey: Readonly<Record<string, string>>;
  readonly movesSecret: {
    readonly address: readonly string[];
    readonly settings: readonly string[];
    readonly tlsStrength: readonly string[];
  };
  readonly outcomes: readonly ConnectionTestOutcome[];
  readonly limits: { readonly caBytes: number; readonly secretValueBytes: number };
}

const MIRROR = mirror as unknown as Mirror;

/** The kinds in the order the reference lists them, and the Add form offers them. */
export const KIND_ORDER: readonly ConnectionKind[] = MIRROR.order;

/** The test outcomes in the order the reference lists them. */
export const OUTCOMES: readonly ConnectionTestOutcome[] = MIRROR.outcomes;

/** The largest CA certificate the server takes, in bytes. */
export const CA_BYTES: number = MIRROR.limits.caBytes;

/** The largest secret value the server takes, in bytes. */
export const SECRET_VALUE_BYTES: number = MIRROR.limits.secretValueBytes;

/** The most secret keys a `tcp` connection's person may name. */
export const FREE_KEYS_MAX: number = (MIRROR.kinds.tcp.secret as { readonly max: number }).max;

/**
 * The `http` auth a settings object holds.
 *
 * @param settings - The settings.
 * @returns The auth, or `none` while the settings name no known one.
 */
function httpAuth(settings: Readonly<Record<string, unknown>>): string {
  const auth = settings.auth;
  return typeof auth === "string" && Object.hasOwn(MIRROR.httpAuth, auth) ? auth : "none";
}

/**
 * The fields a kind asks for.
 *
 * @param kind - The kind.
 * @param settings - The settings so far; for `http` its `auth` decides which settings follow.
 * @returns The address fields and the settings fields, the required ones marked.
 */
export function kindFields(
  kind: ConnectionKind,
  settings: Readonly<Record<string, unknown>>,
): { address: readonly FieldSpec[]; settings: readonly FieldSpec[] } {
  const { address, settings: all } = MIRROR.kinds[kind];
  if (kind !== "http") return { address, settings: all };
  const auth = httpAuth(settings);
  const own = Object.entries(MIRROR.httpAuth).flatMap(([name, keys]) =>
    name === auth ? keys : [],
  );
  const required = MIRROR.httpAuthRequired[auth] ?? [];
  return {
    address,
    settings: all
      .filter(
        (field) =>
          field.key === "auth" || field.key === "ca_certificate" || own.includes(field.key),
      )
      .map((field) => (required.includes(field.key) ? { ...field, required: true } : field)),
  };
}

/**
 * The keys of a connection's secret.
 *
 * @param kind - The kind.
 * @param settings - The settings; for `http` its `auth` decides.
 * @returns The keys it must hold, the keys it may hold, the keys of which exactly one is
 *   needed (`azure`), and whether the person names the keys (`tcp`).
 */
export function secretKeys(
  kind: ConnectionKind,
  settings: Readonly<Record<string, unknown>>,
): {
  required: readonly string[];
  optional: readonly string[];
  oneOf: readonly string[] | null;
  free: boolean;
} {
  const spec = MIRROR.kinds[kind].secret;
  if (spec === null) return { required: [], optional: [], oneOf: null, free: false };
  if (spec.byAuth) {
    const key = MIRROR.httpSecretKey[httpAuth(settings)];
    return { required: key === undefined ? [] : [key], optional: [], oneOf: null, free: false };
  }
  return {
    required: spec.required ?? [],
    optional: spec.optional ?? [],
    oneOf: spec.oneOf ?? null,
    free: spec.free === true,
  };
}

/**
 * Whether a connection of this kind and these settings holds a secret.
 *
 * @param kind - The kind.
 * @param settings - The settings; for `http` its `auth` decides.
 * @returns True when the kind asks for, allows or lets the person name secret keys.
 */
export function takesSecret(
  kind: ConnectionKind,
  settings: Readonly<Record<string, unknown>>,
): boolean {
  const { required, optional, oneOf, free } = secretKeys(kind, settings);
  return free || oneOf !== null || required.length > 0 || optional.length > 0;
}

/**
 * Compare a value as the server does: an absent key and a null are the same.
 *
 * @param before - The value before.
 * @param after - The value after.
 * @returns True when they are the same.
 */
function same(before: unknown, after: unknown): boolean {
  return (before ?? null) === (after ?? null);
}

/**
 * How strongly a `tls` value checks the server.
 *
 * @param settings - The settings.
 * @returns The value's place in the strength order; an unknown value counts as the weakest, as on the server.
 */
function tlsStrength(settings: Readonly<Record<string, unknown>>): number {
  const tls = typeof settings.tls === "string" ? settings.tls : "verify-full";
  return Math.max(0, MIRROR.movesSecret.tlsStrength.indexOf(tls));
}

/**
 * The AWS endpoint of a region, the default of an `s3` connection's `endpoint`.
 *
 * @param region - The region.
 * @returns The endpoint.
 */
function s3DefaultEndpoint(region: unknown): string {
  return `https://s3.${text(region)}.amazonaws.com`;
}

/**
 * Whether a change could send a bound secret somewhere else or trust the place less, so that
 * the server asks for the secret's values again (or for the right to manage secrets).
 *
 * @remarks
 * This is the server's `moves_secret`, plus the `s3` region rule: the server moves a default
 * endpoint with a new region, so a new `region` while the endpoint is the old region's default
 * (or not set) is an address change; an endpoint left empty means its region's default.
 * @param kind - The kind.
 * @param before - The connection as stored.
 * @param after - The connection as the form would send it.
 * @returns True when the secret would move.
 */
export function movesSecret(kind: ConnectionKind, before: Parts, after: Parts): boolean {
  const { address, settings } = MIRROR.movesSecret;
  const seen = (parts: Parts): Readonly<Record<string, unknown>> =>
    kind === "s3" ? { ...parts.address, endpoint: s3Endpoint(parts.address) } : parts.address;
  const [was, now] = [seen(before), seen(after)];
  if (address.some((key) => !same(was[key], now[key]))) return true;
  if (settings.some((key) => !same(before.settings[key], after.settings[key]))) return true;
  if (tlsStrength(after.settings) < tlsStrength(before.settings)) return true;
  return (
    kind === "s3" &&
    !same(before.address.region, after.address.region) &&
    was.endpoint === s3DefaultEndpoint(before.address.region) &&
    now.endpoint === was.endpoint
  );
}

/**
 * The endpoint an `s3` address means: the one it names, else its region's default.
 *
 * @param address - The address.
 * @returns The endpoint.
 */
function s3Endpoint(address: Readonly<Record<string, unknown>>): string {
  const endpoint = text(address.endpoint);
  return endpoint === "" ? s3DefaultEndpoint(address.region) : endpoint;
}

/**
 * A value of an address as text.
 *
 * @param value - The value.
 * @returns The text; empty for an absent or non-text value.
 */
function text(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

/**
 * Where a connection points, in one line.
 *
 * @remarks
 * The values are the person's; show the result through `VisibleText` so a hidden character is
 * marked.
 * @param kind - The kind.
 * @param address - The address.
 * @returns `host:port/database`, `host:port`, the base URL, `s3://bucket (region)`,
 *   `gs://bucket`, `account/container` or `root:path`.
 */
export function whereText(
  kind: ConnectionKind,
  address: Readonly<Record<string, unknown>>,
): string {
  const a = (key: string): string => text(address[key]);
  switch (kind) {
    case "postgresql":
    case "mariadb":
      return `${a("host")}:${a("port")}/${a("database")}`;
    case "tcp":
      return `${a("host")}:${a("port")}`;
    case "http":
      return a("base_url");
    case "s3":
      return `s3://${a("bucket")} (${a("region")})`;
    case "gcs":
      return `gs://${a("bucket")}`;
    case "azure":
      return `${a("account")}/${a("container")}`;
    case "sqlite-file":
    case "directory":
      return `${a("root")}:${a("path")}`;
  }
}

/**
 * Whether this build knows a kind: a newer server may have kinds it does not.
 *
 * @param kind - The kind, as the server sent it.
 * @returns True for one of the nine kinds.
 */
export function isKnownKind(kind: string): boolean {
  return (KIND_ORDER as readonly string[]).includes(kind);
}
