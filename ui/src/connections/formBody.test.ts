import { describe, expect, it } from "vitest";

import { createBody, emptyForm, type FormValues } from "./formBody";

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
