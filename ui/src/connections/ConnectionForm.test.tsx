import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConnectionEnvironmentOut } from "../api";
import { MeContext } from "../shell/me";
import { aConnection, ENVIRONMENTS_ROUTE } from "../test/connections";
import { fakeServer, type FakeRoute, type FakeServer } from "../test/server";
import { MemoryStorage } from "../test/storage";
import { fakeWebAuthn } from "../test/webauthn";
import ConnectionForm from "./ConnectionForm";
import { AreaScreen } from "./ConnectionsScreen";
import { writeFilter } from "./remember";

const CREATE = "POST /api/v1/environments/prod/connections";
const MANAGER: ConnectionEnvironmentOut[] = [
  { name: "dev", actions: ["create", "test", "name_secret"] },
  { name: "prod", actions: ["create", "test", "name_secret"] },
];
const ME = { name: "kim.lee", admin: false, noAccessYet: false, mayDeploy: false, picture: null };
const PASSWORD = "Tr0ub4dor-and-3-horses";
const STEP_UP: Record<string, FakeRoute> = {
  "POST /api/v1/sign-in/step-up/options": {
    status: 200,
    body: { ceremony_id: "c-1", public_key: {} },
  },
  "POST /api/v1/sign-in/step-up": { status: 204 },
};

/**
 * Shows where the router is, for the test to read.
 *
 * @returns The path, in an output element.
 */
function Where(): React.JSX.Element {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}</output>;
}

/**
 * Renders the Add form as `kim.lee`, at `/connections/new`, with the connection's page behind
 * it.
 *
 * @param environments - The environments the server lists.
 * @param routes - More routes.
 * @returns The fake server and a user-event session.
 */
function renderAdd(
  environments: readonly ConnectionEnvironmentOut[] = MANAGER,
  routes: Record<string, FakeRoute> = {},
): { server: FakeServer; user: UserEvent } {
  const server = fakeServer({
    [ENVIRONMENTS_ROUTE]: { status: 200, body: environments },
    "GET /api/v1/environments/dev/connections/crm": {
      status: 200,
      body: aConnection({ environment: "dev", last_test: null }),
    },
    ...routes,
  });
  const user = userEvent.setup();
  render(
    <MeContext value={ME}>
      <MemoryRouter initialEntries={["/connections/new"]}>
        <Routes>
          <Route path="/connections/*" element={<AreaScreen />} />
        </Routes>
        <Where />
      </MemoryRouter>
    </MeContext>,
  );
  return { server, user };
}

/**
 * The requests that change something, as `METHOD path`.
 *
 * @param server - The fake server.
 * @returns The writes.
 */
function writes(server: FakeServer): string[] {
  return server.sent
    .filter((request) => request.method !== "GET")
    .map((request) => `${request.method} ${request.path}`);
}

/**
 * Picks an option of a Select.
 *
 * @param user - The user-event session.
 * @param button - The Select's button name.
 * @param option - The option's name.
 */
async function choose(user: UserEvent, button: RegExp, option: string): Promise<void> {
  await user.click(screen.getByRole("button", { name: button }));
  await user.click(await screen.findByRole("option", { name: option }));
}

/**
 * Fills a PostgreSQL connection `crm` with its password.
 *
 * @param user - The user-event session.
 */
async function fillPostgres(user: UserEvent): Promise<void> {
  await user.type(await screen.findByRole("textbox", { name: "Name" }), "crm");
  await user.type(screen.getByRole("textbox", { name: "host" }), "db.example.org");
  await user.type(screen.getByRole("textbox", { name: "database" }), "crm");
  await user.type(screen.getByRole("textbox", { name: "user" }), "loader");
  await user.type(screen.getByLabelText("password"), PASSWORD);
}

/**
 * Serves the create: a step-up first when asked, then the new connection.
 *
 * @param stepUpFirst - Whether the first try is refused with StepUpRequired.
 * @returns The route.
 */
function created(stepUpFirst = false): FakeRoute {
  let tries = 0;
  return () => {
    tries += 1;
    return stepUpFirst && tries === 1
      ? { status: 403, body: { type: "StepUpRequired", detail: "Touch your passkey." } }
      : { status: 201, body: aConnection({ environment: "dev", last_test: null }) };
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ConnectionForm, add", () => {
  it("asks the environment and the kind first, and the kinds are the nine with a line each", async () => {
    renderAdd();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Add connection" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connections" })).toHaveAttribute(
      "href",
      "/connections",
    );
    const environment = await screen.findByRole("button", { name: "Environment: dev" });
    const kinds = screen.getByRole("radiogroup", { name: "Kind" });
    const name = screen.getByRole("textbox", { name: "Name" });
    expect(environment.compareDocumentPosition(kinds)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(kinds.compareDocumentPosition(name)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    const radios = within(kinds).getAllByRole("radio");
    expect(radios.map((radio) => radio.closest("label")?.textContent)).toEqual([
      "PostgreSQL",
      "MariaDB",
      "SQLite file",
      "HTTP API",
      "S3",
      "Google Cloud Storage",
      "Azure Blob Storage",
      "Directory",
      "TCP",
    ]);
    expect(within(kinds).getByRole("radio", { name: "PostgreSQL" })).toBeChecked();
    expect(within(kinds).getByRole("radio", { name: "TCP" })).toHaveAccessibleDescription(
      "Any host and port, for your own client, with secret keys of your choice.",
    );
    expect(within(kinds).getByRole("radio", { name: "S3" })).toHaveAccessibleDescription(
      "An S3 bucket, or a store that speaks S3, with an access key.",
    );
  });

  it("offers only environments that allow create, starting at the list's filter", async () => {
    const storage = new MemoryStorage();
    vi.spyOn(window, "localStorage", "get").mockReturnValue(storage);
    writeFilter("kim.lee", "prod", storage);
    const { user } = renderAdd([
      { name: "dev", actions: ["test"] },
      { name: "prod", actions: ["create", "test"] },
      { name: "staging", actions: ["create"] },
    ]);

    await user.click(await screen.findByRole("button", { name: "Environment: prod" }));

    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "prod",
      "staging",
    ]);
  });

  it("says who may add a connection when no environment allows it", async () => {
    renderAdd([{ name: "prod", actions: ["test"] }]);

    expect(
      await screen.findByText(
        "You may not add connections in any environment. Ask an administrator for MANAGE_CONNECTION.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("offers Try again when the environments cannot be read", async () => {
    const { server, user } = renderAdd(MANAGER, {
      [ENVIRONMENTS_ROUTE]: { status: 500, body: { detail: "The database is not reachable." } },
    });

    await user.click(await screen.findByRole("button", { name: "Try again" }));
    server.route({ [ENVIRONMENTS_ROUTE]: { status: 200, body: MANAGER } });
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("radiogroup", { name: "Kind" })).toBeInTheDocument();
  });

  it("shows only the chosen kind's fields, and http's follow auth", async () => {
    const { user } = renderAdd();
    const kinds = await screen.findByRole("radiogroup", { name: "Kind" });

    expect(screen.getByRole("textbox", { name: "host" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "port (optional)" })).toHaveAttribute(
      "placeholder",
      "5432",
    );
    expect(screen.getByRole("button", { name: /^tls/ })).toHaveTextContent(
      "encrypted, certificate checked",
    );

    await user.click(within(kinds).getByRole("radio", { name: "HTTP API" }));

    expect(screen.queryByRole("textbox", { name: "host" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "base_url" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "username" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Secret" })).not.toBeInTheDocument();

    await choose(user, /^auth/, "basic");

    expect(screen.getByRole("textbox", { name: "username" })).toBeRequired();
    expect(screen.getByLabelText("password")).toBeRequired();

    await choose(user, /^auth/, "API key");

    expect(screen.queryByRole("textbox", { name: "username" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "api_key_name" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "In a header" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "In the query" }));
    expect(screen.getByRole("radio", { name: "In the query" })).toBeChecked();
    expect(screen.getByLabelText("api_key")).toBeInTheDocument();

    await user.click(within(kinds).getByRole("radio", { name: "S3" }));

    expect(
      screen.getByRole("textbox", { name: "endpoint (optional)" }),
    ).toHaveAccessibleDescription("Leave it empty for the region's default.");
    expect(screen.getByLabelText("secret_access_key")).toBeRequired();
    expect(screen.getByLabelText("session_token (optional)")).not.toBeRequired();

    await user.click(within(kinds).getByRole("radio", { name: "Directory" }));

    expect(screen.getByRole("textbox", { name: "root" })).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup", { name: "Secret" })).not.toBeInTheDocument();
  });

  it("offers an existing secret only where the environment allows name_secret", async () => {
    const { user, server } = renderAdd([
      { name: "dev", actions: ["create"] },
      { name: "prod", actions: ["create", "name_secret"] },
    ]);

    await screen.findByRole("radiogroup", { name: "Kind" });
    expect(screen.queryByRole("radiogroup", { name: "Secret" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("password")).toBeInTheDocument();

    await choose(user, /^Environment/, "prod");
    const secret = screen.getByRole("radiogroup", { name: "Secret" });
    expect(within(secret).getByRole("radio", { name: "New values" })).toBeChecked();
    await user.click(within(secret).getByRole("radio", { name: "An existing secret of prod" }));

    expect(screen.queryByLabelText("password")).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Secret name" }), "crm-password");

    // Another kind keeps the secret's name; New values starts empty.
    await user.click(screen.getByRole("radio", { name: "MariaDB" }));
    expect(screen.getByRole("textbox", { name: "Secret name" })).toHaveValue("crm-password");
    await user.click(screen.getByRole("radio", { name: "PostgreSQL" }));
    await user.click(screen.getByRole("radio", { name: "New values" }));
    expect(screen.getByLabelText("password")).toHaveValue("");
    await user.click(screen.getByRole("radio", { name: "An existing secret of prod" }));
    expect(screen.getByRole("textbox", { name: "Secret name" })).toHaveValue("");

    await choose(user, /^Environment/, "dev");
    expect(screen.queryByRole("textbox", { name: "Secret name" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("password")).toBeInTheDocument();

    await choose(user, /^Environment/, "prod");
    await user.click(screen.getByRole("radio", { name: "An existing secret of prod" }));
    await user.type(screen.getByRole("textbox", { name: "Secret name" }), "crm-password");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "crm");
    await user.type(screen.getByRole("textbox", { name: "host" }), "db.example.org");
    await user.type(screen.getByRole("textbox", { name: "database" }), "crm");
    await user.type(screen.getByRole("textbox", { name: "user" }), "loader");
    server.route({ [CREATE]: created() });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(writes(server)).toEqual([CREATE]);
    });
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      name: "crm",
      kind: "postgresql",
      address: { host: "db.example.org", database: "crm" },
      settings: { user: "loader" },
      secret: "crm-password",
    });
  });

  it("each secret value input is a password field with autocomplete off", async () => {
    const { user } = renderAdd();
    const kinds = await screen.findByRole("radiogroup", { name: "Kind" });
    // Every input of the secret but the radios, the key file and a tcp key's name.
    const passwordFields = (): HTMLElement[] => {
      const names = screen.queryAllByRole("textbox", { name: "Key name" });
      return [
        ...document.querySelectorAll<HTMLElement>(".secret-fields input:not([type=radio])"),
      ].filter((input) => input.getAttribute("type") !== "file" && !names.includes(input));
    };
    const check = (count: number): void => {
      const inputs = passwordFields();
      expect(inputs).toHaveLength(count);
      for (const input of inputs) {
        expect(input).toHaveAttribute("type", "password");
        expect(input).toHaveAttribute("autocomplete", "off");
      }
    };

    check(1);
    await user.click(within(kinds).getByRole("radio", { name: "S3" }));
    check(2);
    await user.click(within(kinds).getByRole("radio", { name: "Google Cloud Storage" }));
    check(1);
    await user.click(within(kinds).getByRole("radio", { name: "Azure Blob Storage" }));
    check(1);
    expect(screen.getByLabelText("account_key")).toBeRequired();
    await user.click(screen.getByRole("radio", { name: "SAS token" }));
    expect(screen.queryByLabelText("account_key")).not.toBeInTheDocument();
    check(1);
    await user.click(within(kinds).getByRole("radio", { name: "TCP" }));
    check(0);
    await user.click(screen.getByRole("button", { name: "Add a key" }));
    await user.click(screen.getByRole("button", { name: "Add a key" }));
    expect(screen.getAllByRole("textbox", { name: "Key name" })).toHaveLength(2);
    // The key's name is not a secret; its value is.
    check(2);
  });

  it("checks the name on blur with the server's sentence", async () => {
    const { user } = renderAdd();
    const name = await screen.findByRole("textbox", { name: "Name" });
    const rule =
      "Connection names are lower-case letters, digits and '-', start with a letter, and have at most 63 characters.";

    await user.type(name, "CRM_main");
    expect(screen.queryByText(rule)).not.toBeInTheDocument();
    await user.tab();

    expect(name).toHaveAccessibleDescription(rule);
    expect(name).toHaveAttribute("aria-invalid", "true");

    await user.clear(name);
    await user.type(name, "crm-main");
    await user.tab();

    expect(screen.queryByText(rule)).not.toBeInTheDocument();
    expect(name).not.toHaveAttribute("aria-invalid");
  });

  it("checks a tcp key's name, keeps at most 16 keys, and removes one", async () => {
    const { user, server } = renderAdd();
    const kinds = await screen.findByRole("radiogroup", { name: "Kind" });
    await user.click(within(kinds).getByRole("radio", { name: "TCP" }));
    const add = screen.getByRole("button", { name: "Add a key" });

    await user.click(add);
    const [key] = screen.getAllByRole("textbox", { name: "Key name" });
    await user.type(key as HTMLElement, "Api-User");
    await user.tab();
    expect(key).toHaveAccessibleDescription(
      "Key names are lower-case letters, digits and '_', start with a letter, and have at most 32 characters.",
    );
    await user.clear(key as HTMLElement);
    await user.type(key as HTMLElement, "api_user");
    await user.tab();
    expect(key).not.toHaveAttribute("aria-invalid");
    await user.type(screen.getAllByLabelText("Value")[0] as HTMLElement, "loader");

    for (let row = 1; row < 16; row += 1) {
      await user.click(add);
    }
    expect(screen.getAllByRole("textbox", { name: "Key name" })).toHaveLength(16);
    expect(screen.queryByRole("button", { name: "Add a key" })).not.toBeInTheDocument();
    expect(screen.getByText("A connection holds at most 16 secret keys.")).toBeInTheDocument();

    const removes = screen.getAllByRole("button", { name: "Remove this key" });
    for (const remove of removes.slice(1)) {
      await user.click(remove);
    }
    expect(screen.getAllByRole("textbox", { name: "Key name" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Add a key" }));
    const second = screen.getAllByRole("textbox", { name: "Key name" })[1] as HTMLElement;
    await user.type(second, "api_user");
    await user.type(screen.getAllByLabelText("Value")[1] as HTMLElement, "again");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "ledger");
    await user.type(screen.getByRole("textbox", { name: "host" }), "ledger.internal");
    await user.type(screen.getByRole("textbox", { name: "port" }), "7000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(second).toHaveAccessibleDescription("Each key is named once.");
    expect(writes(server)).toEqual([]);

    await user.click(screen.getAllByRole("button", { name: "Remove this key" })[1] as HTMLElement);
    server.route({ "POST /api/v1/environments/dev/connections": created() });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(writes(server)).toEqual(["POST /api/v1/environments/dev/connections"]);
    });
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      name: "ledger",
      kind: "tcp",
      address: { host: "ledger.internal", port: 7000 },
      settings: {},
      secret_values: { api_user: "loader" },
    });
  });

  it("reads a CA certificate file without showing it, and refuses one over 64 KiB", async () => {
    const { user, server } = renderAdd();
    const pem =
      "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ2E=\n-----END CERTIFICATE-----\n";
    const file = await screen.findByLabelText("ca_certificate (optional)");
    expect(file).toHaveAttribute("type", "file");
    expect(file).toHaveAttribute("accept", ".pem,.crt,.cer");

    // A dialog closed without a file changes nothing.
    fireEvent.change(file, { target: { files: [] } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await user.upload(file, new File(["x".repeat(65_537)], "huge.pem"));
    expect(await screen.findByText("A CA certificate file holds at most 64 KiB.")).toBeVisible();
    expect(file).toHaveValue("");

    await user.upload(file, new File([pem], "ca.pem"));
    expect(await screen.findByText("A CA certificate is read; Save sends it.")).toBeInTheDocument();
    expect(
      screen.queryByText("A CA certificate file holds at most 64 KiB."),
    ).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("MIIBszCC");

    await user.click(screen.getByRole("button", { name: "Remove the CA certificate" }));
    expect(screen.queryByText("A CA certificate is read; Save sends it.")).not.toBeInTheDocument();
    expect(file).toHaveFocus();

    await user.upload(file, new File([pem], "ca.pem"));
    await screen.findByText("A CA certificate is read; Save sends it.");
    await fillPostgres(user);
    server.route({ "POST /api/v1/environments/dev/connections": created() });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(writes(server)).toEqual(["POST /api/v1/environments/dev/connections"]);
    });
    expect(server.sent.find((request) => request.method === "POST")?.body).toMatchObject({
      settings: { user: "loader", ca_certificate: pem },
    });
  });

  it("says so when a file cannot be read", async () => {
    const { user } = renderAdd();
    const file = await screen.findByLabelText("ca_certificate (optional)");
    const broken = new File(["x"], "ca.pem");
    vi.spyOn(broken, "text").mockRejectedValue(new DOMException("gone", "NotReadableError"));

    await user.upload(file, broken);

    expect(
      await screen.findByText("The file could not be read; choose it again."),
    ).toBeInTheDocument();
  });

  it("reads a service account key from a file into the field, without showing it", async () => {
    const { user, server } = renderAdd();
    const kinds = await screen.findByRole("radiogroup", { name: "Kind" });
    await user.click(within(kinds).getByRole("radio", { name: "Google Cloud Storage" }));
    const key = '{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----x"}';
    const file = screen.getByLabelText("Read the key from a file…");

    await user.upload(file, new File(["y".repeat(65_537)], "huge.json"));
    expect(await screen.findByText("A key file holds at most 64 KiB.")).toBeVisible();
    expect(screen.getByLabelText("service_account_json")).toHaveValue("");

    await user.upload(file, new File([key], "key.json"));
    await waitFor(() => {
      expect(screen.getByLabelText("service_account_json")).not.toHaveValue("");
    });
    expect(screen.getByLabelText("service_account_json")).toHaveAttribute("type", "password");
    expect(document.body.textContent).not.toContain("PRIVATE KEY");
    await user.type(screen.getByRole("textbox", { name: "Name" }), "events");
    await user.type(screen.getByRole("textbox", { name: "bucket" }), "lakana-events");
    server.route({ "POST /api/v1/environments/dev/connections": created() });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(writes(server)).toEqual(["POST /api/v1/environments/dev/connections"]);
    });
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      name: "events",
      kind: "gcs",
      address: { bucket: "lakana-events" },
      settings: {},
      secret_values: { service_account_json: key },
    });
  });

  it("sends the secret only in the create and forgets it after the save", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
      vi.spyOn(console, level),
    );
    const { user, server } = renderAdd(MANAGER, {
      "POST /api/v1/environments/dev/connections": created(),
    });
    await fillPostgres(user);

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("button", { name: "Test now" })).toBeInTheDocument();
    const carrying = server.sent.filter((request) =>
      JSON.stringify([request.path, request.headers, request.body]).includes(PASSWORD),
    );
    expect(carrying.map((request) => `${request.method} ${request.path}`)).toEqual([
      "POST /api/v1/environments/dev/connections",
    ]);
    expect(carrying[0]?.body).toMatchObject({ secret_values: { password: PASSWORD } });
    expect(document.body.textContent).not.toContain(PASSWORD);
    for (const input of document.querySelectorAll("input, textarea")) {
      expect((input as HTMLInputElement).value).not.toContain(PASSWORD);
    }
    expect(screen.getByTestId("where")).not.toHaveTextContent(PASSWORD);
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = String(storage.key(index));
        expect(`${key}=${String(storage.getItem(key))}`).not.toContain(PASSWORD);
      }
    }
    for (const spy of spies) {
      expect(JSON.stringify(spy.mock.calls.map(String))).not.toContain(PASSWORD);
    }
  });

  it("asks for a passkey touch and sends again", async () => {
    const webauthn = fakeWebAuthn();
    const { user, server } = renderAdd(MANAGER, {
      ...STEP_UP,
      "POST /api/v1/environments/dev/connections": created(true),
    });
    await fillPostgres(user);

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("button", { name: "Test now" })).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(writes(server)).toEqual([
      "POST /api/v1/environments/dev/connections",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/environments/dev/connections",
    ]);
  });

  it("without a passkey says to add one", async () => {
    fakeWebAuthn();
    const { user } = renderAdd(MANAGER, {
      "POST /api/v1/environments/dev/connections": created(true),
      "POST /api/v1/sign-in/step-up/options": {
        status: 409,
        body: { type: "NoPasskeyError", detail: "You have no passkey." },
      },
    });
    await fillPostgres(user);

    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Your account has no passkey yet, and this action needs a passkey touch.",
    );
    expect(within(alert).getByRole("link", { name: "Add a passkey" })).toHaveAttribute(
      "href",
      "/account",
    );
    expect(screen.getByLabelText("password")).toHaveValue(PASSWORD);
  });

  it("shows the server's 422 above the form and keeps the fields", async () => {
    const { user, server } = renderAdd(MANAGER, {
      "POST /api/v1/environments/dev/connections": {
        status: 422,
        body: {
          type: "ValidationError",
          detail: "address.host: db .example.org is not a host name.",
        },
      },
    });
    await fillPostgres(user);

    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("address.host: db .example.org is not a host name.");
    expect(alert.compareDocumentPosition(screen.getByRole("textbox", { name: "host" }))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(alert).not.toHaveTextContent(PASSWORD);
    expect(screen.getByRole("textbox", { name: "host" })).toHaveValue("db.example.org");
    expect(screen.getByLabelText("password")).toHaveValue(PASSWORD);
    expect(screen.getByTestId("where")).toHaveTextContent("/connections/new");
    expect(writes(server)).toEqual(["POST /api/v1/environments/dev/connections"]);
  });

  it("sends nothing while a required field is empty", async () => {
    const { user, server } = renderAdd();
    await user.type(await screen.findByRole("textbox", { name: "Name" }), "crm");

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(writes(server)).toEqual([]);
    expect(screen.getByRole("textbox", { name: "host" })).toHaveAttribute("aria-invalid", "true");
  });

  it("after the create goes to the page with the note and Test now", async () => {
    const { user } = renderAdd(MANAGER, {
      "POST /api/v1/environments/dev/connections": created(),
    });
    await fillPostgres(user);

    await user.click(screen.getByRole("button", { name: "Save" }));

    const note = await screen.findByText(
      "Created crm. Test it now to see that a worker can reach it.",
    );
    expect(note).toHaveAttribute("role", "status");
    expect(screen.getByTestId("where")).toHaveTextContent("/connections/dev/crm");
    await waitFor(() => {
      expect(note).toHaveFocus();
    });
    expect(await screen.findByRole("button", { name: "Test now" })).toHaveClass("button-primary");
  });
});

describe("ConnectionForm, edit", () => {
  it("is not built yet, and says the page does not exist", () => {
    render(
      <MemoryRouter>
        <ConnectionForm mode="edit" connection={aConnection()} />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });
});
