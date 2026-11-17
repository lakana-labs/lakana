import { describe, expect, it } from "vitest";

import { aConnection } from "../test/connections";
import {
  createBody,
  editForm,
  emptyForm,
  type FormValues,
  movesWith,
  updateBody,
} from "./formBody";

/**
 * A form as Add holds it, in `prod`.
 *
 * @param over - The values that differ from an empty PostgreSQL form.
 * @returns The form.
 */
function form(over: Partial<FormValues>): FormValues {
  return { ...emptyForm("prod", "postgresql"), name: "crm", ...over };
}

describe("createBody", () => {
  it("a postgresql create sends host, database, user and the password, and leaves out an empty port", () => {
    expect(
      createBody(
        form({
          address: { host: "db.example.org", port: "", database: "crm" },
          settings: { user: "loader", tls: "" },
          secret: { way: "values", values: { password: "s3cr3t-Value" } },
        }),
      ),
    ).toEqual({
      name: "crm",
      kind: "postgresql",
      address: { host: "db.example.org", database: "crm" },
      settings: { user: "loader" },
      secret_values: { password: "s3cr3t-Value" },
    });
  });

  it("sends a port as a number, and the text of one that is not, for the server to judge", () => {
    const body = (port: string): unknown =>
      createBody(form({ address: { host: "db.example.org", port, database: "crm" } })).address;

    expect(body("6543")).toEqual({ host: "db.example.org", port: 6543, database: "crm" });
    expect(body("65x")).toEqual({ host: "db.example.org", port: "65x", database: "crm" });
  });

  it("an http create sends only the fields of its auth", () => {
    expect(
      createBody(
        form({
          kind: "http",
          name: "weather",
          address: { base_url: "https://api.example.org" },
          settings: {
            auth: "bearer",
            username: "left-from-basic",
            api_key_name: "X-Key",
            client_id: "lakana",
          },
          secret: { way: "values", values: { token: "tok-123", password: "from-basic" } },
        }),
      ),
    ).toEqual({
      name: "weather",
      kind: "http",
      address: { base_url: "https://api.example.org" },
      settings: { auth: "bearer" },
      secret_values: { token: "tok-123" },
    });
  });

  it("an http create without auth sends no secret", () => {
    expect(
      createBody(
        form({
          kind: "http",
          address: { base_url: "https://api.example.org" },
          settings: {},
          secret: { way: "values", values: { token: "tok-123" } },
        }),
      ),
    ).toEqual({
      name: "crm",
      kind: "http",
      address: { base_url: "https://api.example.org" },
      settings: {},
    });
  });

  it("an azure create sends exactly the chosen key", () => {
    expect(
      createBody(
        form({
          kind: "azure",
          name: "landing",
          address: { account: "lakanadata", container: "landing" },
          settings: { prefix: "" },
          secret: { way: "values", values: { sas_token: "sv=2024&sig=abc", account_key: "" } },
        }),
      ),
    ).toEqual({
      name: "landing",
      kind: "azure",
      address: { account: "lakanadata", container: "landing" },
      settings: {},
      secret_values: { sas_token: "sv=2024&sig=abc" },
    });
  });

  it("an azure create sends one key even when the form holds both", () => {
    const sent = (values: Record<string, string>): unknown =>
      createBody(
        form({
          kind: "azure",
          address: { account: "lakanadata", container: "landing" },
          secret: { way: "values", values },
        }),
      ).secret_values;

    expect(sent({ account_key: "a2V5", sas_token: "sv=2024&sig=abc" })).toEqual({
      account_key: "a2V5",
    });
    expect(sent({ sas_token: "sv=2024&sig=abc" })).toEqual({ sas_token: "sv=2024&sig=abc" });
  });

  it("a tcp create sends at most 16 keys", () => {
    const values = Object.fromEntries(
      Array.from({ length: 17 }, (_, index) => [`key_${String(index + 1)}`, "v"]),
    );

    const sent = createBody(
      form({
        kind: "tcp",
        address: { host: "ledger.internal", port: "7000" },
        secret: { way: "values", values },
      }),
    ).secret_values;

    expect(Object.keys(sent ?? {})).toHaveLength(16);
    expect(sent).not.toHaveProperty("key_17");
  });

  it("a tcp create sends the named keys", () => {
    expect(
      createBody(
        form({
          kind: "tcp",
          name: "ledger",
          address: { host: "ledger.internal", port: "7000" },
          secret: { way: "values", values: { api_user: "loader", api_pass: "p4ss", empty: "" } },
        }),
      ),
    ).toEqual({
      name: "ledger",
      kind: "tcp",
      address: { host: "ledger.internal", port: 7000 },
      settings: {},
      secret_values: { api_user: "loader", api_pass: "p4ss" },
    });
  });

  it("a named secret sends secret and no values", () => {
    expect(
      createBody(
        form({
          address: { host: "db.example.org", database: "crm" },
          settings: { user: "loader" },
          secret: { way: "named", name: "crm-password" },
        }),
      ),
    ).toEqual({
      name: "crm",
      kind: "postgresql",
      address: { host: "db.example.org", database: "crm" },
      settings: { user: "loader" },
      secret: "crm-password",
    });
  });

  it("an s3 create leaves out an empty endpoint and an empty session token", () => {
    expect(
      createBody(
        form({
          kind: "s3",
          name: "raw",
          address: { bucket: "lakana-raw", region: "eu-central-1", endpoint: "" },
          settings: { access_key_id: "AKIAEXAMPLE", prefix: "" },
          secret: { way: "values", values: { secret_access_key: "abc/def", session_token: "" } },
        }),
      ),
    ).toEqual({
      name: "raw",
      kind: "s3",
      address: { bucket: "lakana-raw", region: "eu-central-1" },
      settings: { access_key_id: "AKIAEXAMPLE" },
      secret_values: { secret_access_key: "abc/def" },
    });
  });

  it("sends a new CA certificate as its text, and nothing for none", () => {
    const pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n";
    const base = { address: { host: "db.example.org", database: "crm" }, settings: { user: "u" } };

    expect(createBody(form({ ...base, ca: { kind: "new", pem } })).settings).toEqual({
      user: "u",
      ca_certificate: pem,
    });
    expect(createBody(form({ ...base, ca: { kind: "keep" } })).settings).toEqual({ user: "u" });
  });

  it("a kind without a secret, or a form without values, sends none", () => {
    expect(
      createBody(
        form({
          kind: "directory",
          address: { root: "landing", path: "crm" },
          secret: { way: "values", values: { password: "stray" } },
        }),
      ),
    ).toEqual({
      name: "crm",
      kind: "directory",
      address: { root: "landing", path: "crm" },
      settings: {},
    });
    expect(createBody(form({ secret: { way: "values", values: {} } }))).not.toHaveProperty(
      "secret_values",
    );
    expect(createBody(form({ secret: { way: "none" } }))).not.toHaveProperty("secret_values");
  });
});

/** A stored PostgreSQL connection with a secret and a CA certificate. */
const STORED = aConnection({
  address: { host: "db.example.org", port: 5432, database: "crm" },
  settings: { user: "loader", tls: "require", ca_certificate: "sha256:ab12" },
});

/**
 * The Edit form of {@link STORED}, changed.
 *
 * @param over - The values that differ from the stored connection.
 * @returns The form.
 */
function edited(over: Partial<FormValues>): FormValues {
  return { ...editForm(STORED), ...over };
}

describe("editForm", () => {
  it("starts from the connection, keeps its secret and CA certificate, and holds no value", () => {
    expect(editForm(STORED)).toEqual({
      environment: "prod",
      kind: "postgresql",
      name: "crm",
      address: { host: "db.example.org", port: "5432", database: "crm" },
      settings: { user: "loader", tls: "require" },
      ca: { kind: "keep" },
      secret: { way: "keep" },
    });
    expect(editForm(aConnection({ secret: null })).secret).toEqual({ way: "none" });
    expect(editForm(aConnection({ settings: { user: "loader", tls: null } })).settings).toEqual({
      user: "loader",
      tls: "",
    });
  });
});

describe("updateBody", () => {
  it("an update sends only what changed", () => {
    expect(
      updateBody(
        STORED,
        edited({
          address: { host: "db2.internal", port: "5432", database: "crm" },
          settings: { user: "etl", tls: "require" },
        }),
      ),
    ).toEqual({ address: { host: "db2.internal" }, settings: { user: "etl" } });
  });

  it("a removed setting is null and an unchanged CA is not sent", () => {
    const pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n";

    expect(
      updateBody(
        STORED,
        edited({ address: { host: "db.example.org", port: "", database: "crm" } }),
      ),
    ).toEqual({ address: { port: null } });
    expect(updateBody(STORED, edited({ settings: { user: "loader", tls: "" } }))).toEqual({
      settings: { tls: null },
    });
    expect(updateBody(STORED, edited({ ca: { kind: "remove" } }))).toEqual({
      settings: { ca_certificate: null },
    });
    expect(updateBody(STORED, edited({ ca: { kind: "new", pem } }))).toEqual({
      settings: { ca_certificate: pem },
    });
  });

  it("stop using a secret sends clear secret", () => {
    expect(updateBody(STORED, edited({ secret: { way: "none" } }))).toEqual({
      clear: ["secret"],
    });
    expect(updateBody(STORED, edited({ secret: { way: "named", name: "crm-2" } }))).toEqual({
      secret: "crm-2",
    });
    expect(
      updateBody(STORED, edited({ secret: { way: "values", values: { password: "n3w" } } })),
    ).toEqual({ secret_values: { password: "n3w" } });
  });

  it("nothing changed gives null", () => {
    expect(updateBody(STORED, editForm(STORED))).toBeNull();
    expect(
      updateBody(STORED, edited({ secret: { way: "named", name: "crm-password" } })),
    ).toBeNull();
    expect(updateBody(STORED, edited({ secret: { way: "values", values: {} } }))).toBeNull();
    const bare = aConnection({ secret: null, settings: { user: "loader" } });
    expect(updateBody(bare, editForm(bare))).toBeNull();
  });

  it("another http auth removes the old auth's settings and, without a secret, unbinds it", () => {
    const http = aConnection({
      kind: "http",
      address: { base_url: "https://api.example.org" },
      settings: { auth: "basic", username: "loader" },
    });

    expect(
      updateBody(http, { ...editForm(http), settings: { auth: "none", username: "loader" } }),
    ).toEqual({ settings: { auth: "none", username: null }, clear: ["secret"] });
  });
});

describe("an s3 endpoint that is its region's default", () => {
  const s3 = aConnection({
    kind: "s3",
    address: {
      bucket: "events",
      region: "eu-west-1",
      endpoint: "https://s3.eu-west-1.amazonaws.com",
    },
    settings: { access_key_id: "AKIAEXAMPLE" },
  });

  it("starts empty, so a new region takes its own default, and still moves the secret", () => {
    const start = editForm(s3);
    expect(start.address).toEqual({ bucket: "events", region: "eu-west-1" });
    expect(updateBody(s3, start)).toBeNull();

    const moved = { ...start, address: { ...start.address, region: "us-east-1" } };
    expect(updateBody(s3, moved)).toEqual({ address: { region: "us-east-1" } });
    expect(movesWith(s3, moved)).toBe(true);
  });

  it("keeps an endpoint of its own", () => {
    const own = aConnection({
      kind: "s3",
      address: { bucket: "events", region: "eu-west-1", endpoint: "https://minio.internal" },
      settings: { access_key_id: "AKIAEXAMPLE" },
    });
    expect(editForm(own).address.endpoint).toBe("https://minio.internal");
  });
});

describe("movesWith", () => {
  it("a new host, a weaker tls or a new CA moves the secret; a new user does not", () => {
    expect(
      movesWith(
        STORED,
        edited({ address: { host: "db2.internal", port: "5432", database: "crm" } }),
      ),
    ).toBe(true);
    expect(movesWith(STORED, edited({ settings: { user: "loader", tls: "disable" } }))).toBe(true);
    expect(movesWith(STORED, edited({ ca: { kind: "remove" } }))).toBe(true);
    expect(movesWith(STORED, edited({ settings: { user: "etl", tls: "require" } }))).toBe(false);
    expect(movesWith(STORED, editForm(STORED))).toBe(false);
  });
});
