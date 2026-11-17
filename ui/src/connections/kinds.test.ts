import i18next from "i18next";
import { describe, expect, it } from "vitest";

import "./connectionStrings";
import {
  KIND_ORDER,
  OUTCOMES,
  kindFields,
  movesSecret,
  secretKeys,
  takesSecret,
  whereText,
  type Parts,
} from "./kinds";

const keys = (fields: readonly { key: string }[]): string[] => fields.map((field) => field.key);
const parts = (address: Record<string, unknown>, settings: Record<string, unknown>): Parts => ({
  address,
  settings,
});

describe("the kinds", () => {
  it("every kind has a name and a line, and every outcome a word, a cause and a next step", () => {
    for (const kind of KIND_ORDER) {
      expect(i18next.exists(`connections.kinds.${kind}.name`)).toBe(true);
      expect(i18next.exists(`connections.kinds.${kind}.line`)).toBe(true);
    }
    for (const outcome of OUTCOMES) {
      expect(i18next.exists(`connections.outcomes.${outcome}.word`)).toBe(true);
      expect(i18next.exists(`connections.outcomes.${outcome}.cause`)).toBe(true);
      // `ok` has no next step: there is nothing to do.
      expect(i18next.exists(`connections.outcomes.${outcome}.next`)).toBe(outcome !== "ok");
    }
    for (const word of ["verify-full", "require", "disable"]) {
      expect(i18next.exists(`connections.tls.${word}`)).toBe(true);
    }
    for (const word of ["none", "bearer", "basic", "api_key", "oauth2"]) {
      expect(i18next.exists(`connections.auth.${word}`)).toBe(true);
    }
    expect(KIND_ORDER).toHaveLength(9);
    expect(OUTCOMES).toHaveLength(10);
  });

  it("gives a database its port default, choices and a pem field", () => {
    const { address, settings } = kindFields("postgresql", {});

    expect(address.find((field) => field.key === "port")).toMatchObject({
      required: false,
      default: 5432,
    });
    expect(settings.find((field) => field.key === "tls")?.choices).toEqual([
      "verify-full",
      "require",
      "disable",
    ]);
    expect(settings.find((field) => field.key === "ca_certificate")?.pem).toBe(true);
    expect(keys(kindFields("tcp", {}).settings)).toEqual([]);
  });

  it("http's settings follow its auth", () => {
    expect(keys(kindFields("http", {}).settings)).toEqual(["auth", "ca_certificate"]);
    expect(keys(kindFields("http", { auth: "bearer" }).settings)).toEqual([
      "auth",
      "ca_certificate",
    ]);
    expect(keys(kindFields("http", { auth: "basic" }).settings)).toEqual([
      "auth",
      "username",
      "ca_certificate",
    ]);
    const oauth = kindFields("http", { auth: "oauth2" }).settings;
    expect(keys(oauth)).toEqual(["auth", "client_id", "token_url", "scope", "ca_certificate"]);
    expect(oauth.filter((field) => field.required).map((field) => field.key)).toEqual([
      "client_id",
      "token_url",
    ]);
    expect(keys(kindFields("http", { auth: "api_key" }).settings)).toContain("api_key_in");
    for (const auth of ["toString", "constructor", "__proto__"]) {
      expect(keys(kindFields("http", { auth }).settings)).toEqual(["auth", "ca_certificate"]);
      expect(secretKeys("http", { auth }).required).toEqual([]);
    }
    expect(keys(kindFields("http", { auth: "mystery" }).settings)).toEqual([
      "auth",
      "ca_certificate",
    ]);
  });

  it("names the secret's keys by kind and auth", () => {
    expect(secretKeys("postgresql", {})).toEqual({
      required: ["password"],
      optional: [],
      oneOf: null,
      free: false,
    });
    expect(secretKeys("s3", {})).toMatchObject({
      required: ["secret_access_key"],
      optional: ["session_token"],
    });
    expect(secretKeys("http", { auth: "oauth2" }).required).toEqual(["client_secret"]);
    expect(secretKeys("http", {}).required).toEqual([]);
    expect(secretKeys("azure", {})).toMatchObject({
      required: [],
      oneOf: ["account_key", "sas_token"],
    });
    expect(secretKeys("tcp", {})).toMatchObject({ free: true, required: [] });
    expect(secretKeys("directory", {})).toEqual({
      required: [],
      optional: [],
      oneOf: null,
      free: false,
    });
  });

  it("takes a secret except for no-auth http and the file kinds", () => {
    expect(takesSecret("postgresql", {})).toBe(true);
    expect(takesSecret("http", { auth: "none" })).toBe(false);
    expect(takesSecret("http", { auth: "bearer" })).toBe(true);
    expect(takesSecret("azure", {})).toBe(true);
    expect(takesSecret("tcp", {})).toBe(true);
    expect(takesSecret("sqlite-file", {})).toBe(false);
    expect(takesSecret("directory", {})).toBe(false);
  });
});

describe("movesSecret", () => {
  const db = parts({ host: "db", port: 5432, database: "x" }, { user: "a", tls: "verify-full" });

  it("a host change moves it, a user change does not", () => {
    expect(
      movesSecret("postgresql", db, parts({ ...db.address, host: "other" }, db.settings)),
    ).toBe(true);
    expect(movesSecret("postgresql", db, parts(db.address, { ...db.settings, user: "b" }))).toBe(
      false,
    );
    expect(movesSecret("postgresql", db, db)).toBe(false);
  });

  it("weakening tls moves it, strengthening does not", () => {
    const strong = parts(db.address, { user: "a", tls: "verify-full" });
    const mid = parts(db.address, { user: "a", tls: "require" });
    expect(movesSecret("postgresql", strong, mid)).toBe(true);
    expect(movesSecret("postgresql", mid, strong)).toBe(false);
    expect(movesSecret("postgresql", mid, parts(db.address, { user: "a" }))).toBe(false);
    expect(movesSecret("postgresql", strong, parts(db.address, { user: "a", tls: "x" }))).toBe(
      true,
    );
  });

  it("an unknown tls counts as the weakest, as on the server", () => {
    const at = (tls: string): Parts => parts(db.address, { user: "a", tls });
    expect(movesSecret("postgresql", at("disable"), at("x"))).toBe(false);
    expect(movesSecret("postgresql", at("x"), at("disable"))).toBe(false);
    expect(movesSecret("postgresql", at("require"), at("x"))).toBe(true);
  });

  it("a new, changed or removed CA moves it", () => {
    const withCa = parts(db.address, { ...db.settings, ca_certificate: "sha256:a" });
    expect(movesSecret("postgresql", db, withCa)).toBe(true);
    expect(movesSecret("postgresql", withCa, db)).toBe(true);
    expect(movesSecret("postgresql", withCa, withCa)).toBe(false);
  });

  it("an absent value and a null are the same", () => {
    expect(
      movesSecret("postgresql", db, parts({ ...db.address }, { ...db.settings, auth: null })),
    ).toBe(false);
  });

  it("an s3 region change moves it only while the endpoint is the old region's default", () => {
    const fallback = parts(
      { bucket: "b", region: "eu-west-1", endpoint: "https://s3.eu-west-1.amazonaws.com" },
      { access_key_id: "k" },
    );
    const custom = parts(
      { bucket: "b", region: "eu-west-1", endpoint: "https://minio.example" },
      { access_key_id: "k" },
    );
    const bare = parts({ bucket: "b", region: "eu-west-1" }, { access_key_id: "k" });
    const moved = (before: Parts): Parts =>
      parts({ ...before.address, region: "us-east-1" }, before.settings);

    expect(movesSecret("s3", fallback, moved(fallback))).toBe(true);
    expect(movesSecret("s3", custom, moved(custom))).toBe(false);
    expect(movesSecret("s3", bare, moved(bare))).toBe(true);
    expect(movesSecret("s3", fallback, fallback)).toBe(false);
    const emptied = (before: Parts, endpoint: string | undefined): Parts =>
      parts({ ...before.address, endpoint }, before.settings);
    // An endpoint left empty means the region's default: the same region is no move.
    expect(movesSecret("s3", fallback, emptied(fallback, ""))).toBe(false);
    expect(movesSecret("s3", fallback, emptied(fallback, undefined))).toBe(false);
    expect(movesSecret("s3", bare, emptied(bare, ""))).toBe(false);
    expect(movesSecret("s3", fallback, emptied(moved(fallback), ""))).toBe(true);
    expect(movesSecret("gcs", fallback, moved(fallback))).toBe(false);
  });
});

describe("whereText", () => {
  it("names where each kind points", () => {
    expect(whereText("postgresql", { host: "db", port: 5432, database: "crm" })).toBe(
      "db:5432/crm",
    );
    expect(whereText("mariadb", { host: "db", port: 3306, database: "crm" })).toBe("db:3306/crm");
    expect(whereText("tcp", { host: "h", port: 99 })).toBe("h:99");
    expect(whereText("http", { base_url: "https://api.example" })).toBe("https://api.example");
    expect(whereText("s3", { bucket: "b", region: "eu-west-1" })).toBe("s3://b (eu-west-1)");
    expect(whereText("gcs", { bucket: "g" })).toBe("gs://g");
    expect(whereText("azure", { account: "acct", container: "c" })).toBe("acct/c");
    expect(whereText("sqlite-file", { root: "data", path: "a.db" })).toBe("data:a.db");
    expect(whereText("directory", { root: "data", path: "in" })).toBe("data:in");
  });

  it("keeps a hidden character for VisibleText to mark, and survives missing values", () => {
    expect(whereText("postgresql", { host: "crm‮db", port: 1, database: "x" })).toContain("‮");
    expect(whereText("tcp", { host: "h" })).toBe("h:");
    expect(whereText("gcs", {})).toBe("gs://");
  });
});
