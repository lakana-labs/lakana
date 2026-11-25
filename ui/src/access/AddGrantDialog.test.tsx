import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  accessStatusRoute,
  aMember,
  aGrant,
  anEnvironment,
  freshSession,
  STALE_SESSION,
  STEP_UP_ROUTES,
} from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";
import { NO_EFFECT_ACTIONS } from "./AddGrantDialog";

const PATH = "/settings/access/grants";

/**
 * The routes of Grants: dev and prod, nothing managed.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function grantRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...accessStatusRoute(),
    "GET /api/v1/sign-in/session": freshSession(),
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), anEnvironment("prod")],
    },
    "GET /api/v1/grants": { status: 200, body: [] },
    "GET /api/v1/principals": { status: 200, body: [] },
    "GET /api/v1/groups": { status: 200, body: [] },
    ...more,
  };
}

/**
 * Opens Add grant.
 *
 * @param path - The address, with the list's filter.
 * @param routes - The routes.
 * @returns The dialog and the rendered shell.
 */
async function openDialog(path = PATH, routes = grantRoutes()) {
  const shell = renderShell(path, { routes });
  await screen.findByText(/No grant/);
  await shell.user.click(await screen.findByRole("button", { name: "Add grant" }));
  const dialog = await screen.findByRole("dialog", { name: "Add grant" });
  return { ...shell, dialog };
}

/**
 * Chooses an option of one of the dialog's lists.
 *
 * @param shell - The rendered shell and dialog.
 * @param label - The list's label.
 * @param option - The option's name.
 */
async function choose(
  shell: Awaited<ReturnType<typeof openDialog>>,
  label: RegExp,
  option: RegExp | string,
): Promise<void> {
  await shell.user.click(within(shell.dialog).getByRole("button", { name: label }));
  await shell.user.click(await screen.findByRole("option", { name: option }));
}

describe("AddGrantDialog", () => {
  it("suggests people and service accounts, or groups, by the choice of who", async () => {
    const shell = await openDialog(
      PATH,
      grantRoutes({
        "GET /api/v1/principals": { status: 200, body: [aMember("maria"), aMember("marek")] },
        "GET /api/v1/groups": { status: 200, body: [{ name: "marketing" }] },
      }),
    );
    const { user, dialog, server } = shell;
    const name = within(dialog).getByRole("combobox", { name: "Name" });

    await user.type(name, "mar");
    expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
      "mariaPerson",
      "marekPerson",
    ]);
    await user.keyboard("{Escape}");
    await user.click(within(dialog).getByRole("radio", { name: "A group" }));
    await user.click(name);
    await user.keyboard("{ArrowDown}");

    expect(await screen.findByRole("option", { name: "marketing" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /^maria/ })).toBeNull();
    await user.keyboard("{Enter}");
    expect(name).toHaveValue("marketing");
    expect(
      server.sent
        .map((request) => request.path)
        .filter((path) => /^\/api\/v1\/(principals|groups)\?/.test(path)),
    ).toEqual(["/api/v1/principals?limit=20&q=mar", "/api/v1/groups?limit=20&q=mar"]);
  });

  it("says when no group has that name", async () => {
    const { user, dialog } = await openDialog();
    await user.click(within(dialog).getByRole("radio", { name: "A group" }));

    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "anlysts");

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "No group named “anlysts”. Check the spelling.",
      );
    });
  });

  it("grants a role on a namespace", async () => {
    fakeWebAuthn();
    let granted = false;
    const shell = await openDialog(
      PATH,
      grantRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/grants": () => {
          granted = true;
          return { status: 201, body: aGrant() };
        },
        "GET /api/v1/grants": () => ({ status: 200, body: granted ? [aGrant()] : [] }),
      }),
    );
    const { user, dialog, server } = shell;

    await user.click(within(dialog).getByRole("radio", { name: "A group" }));
    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "analysts{Escape}");
    await choose(shell, /Role$/, /^ANALYST/);
    await user.click(within(dialog).getByRole("radio", { name: "A namespace" }));
    await choose(shell, /Environment$/, "dev");
    await user.type(within(dialog).getByRole("textbox", { name: "Namespace" }), "sales");
    const from = server.sent.length;
    await user.click(within(dialog).getByRole("button", { name: "Grant" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    const post = server.sent.find(
      (request) => request.method === "POST" && request.path === "/api/v1/grants",
    );
    expect(post?.body).toEqual({
      group: "analysts",
      role: "ANALYST",
      kind: "NAMESPACE",
      environment: "dev",
      namespace: "sales",
    });
    expect(server.sent[from]?.path).toBe("/api/v1/sign-in/session");
    expect(await screen.findByRole("link", { name: "analysts" })).toBeInTheDocument();
    expect(screen.getByText("Granted.")).toBeInTheDocument();
  });

  it("grants a single action on a table, and on the instance", async () => {
    const shell = await openDialog(
      PATH,
      grantRoutes({ "POST /api/v1/grants": { status: 201, body: aGrant() } }),
    );
    const { user, dialog, server } = shell;

    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "maria{Escape}");
    await user.click(within(dialog).getByRole("radio", { name: "A single action" }));
    await choose(shell, /Action$/, "Read tables");
    await user.click(within(dialog).getByRole("radio", { name: "A table" }));
    await choose(shell, /Environment$/, "dev");
    const grant = within(dialog).getByRole("button", { name: "Grant" });
    await user.type(within(dialog).getByRole("textbox", { name: "Namespace" }), "sales");
    expect(grant).toBeDisabled();
    expect(grant).toHaveAccessibleDescription("Name the table.");
    await user.type(within(dialog).getByRole("textbox", { name: "Table" }), "orders{Enter}");

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      principal: "maria",
      action: "READ_TABLE",
      kind: "TABLE",
      environment: "dev",
      namespace: "sales",
      table: "orders",
    });

    await user.click(screen.getByRole("button", { name: "Add grant" }));
    const again = await screen.findByRole("dialog", { name: "Add grant" });
    await user.type(within(again).getByRole("combobox", { name: "Name" }), "maria{Escape}");
    await user.click(within(again).getByRole("button", { name: /Role$/ }));
    await user.click(await screen.findByRole("option", { name: /^VIEWER/ }));
    await user.click(within(again).getByRole("radio", { name: "The whole instance" }));
    await user.click(within(again).getByRole("button", { name: "Grant" }));
    await waitFor(() => {
      expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(2);
    });
    expect(server.sent.filter((request) => request.method === "POST")[1]?.body).toEqual({
      principal: "maria",
      role: "VIEWER",
      kind: "INSTANCE",
    });
  });

  it("starts from the list's filter", async () => {
    const { dialog } = await openDialog(
      `${PATH}?principal=maria&environment=dev&action=MANAGE_GRANT`,
    );

    expect(
      within(dialog).getByRole("radio", { name: "A person or service account" }),
    ).toBeChecked();
    expect(within(dialog).getByRole("combobox", { name: "Name" })).toHaveValue("maria");
    expect(within(dialog).getByRole("radio", { name: "An environment" })).toBeChecked();
    expect(within(dialog).getByRole("button", { name: /Environment$/ })).toHaveTextContent("dev");
    // An action that does nothing alone is not offered, so the filter's is not taken.
    expect(within(dialog).getByRole("radio", { name: "A single action" })).toBeChecked();
    expect(within(dialog).getByRole("button", { name: /Action$/ })).toHaveTextContent("Choose…");
  });

  it("starts from a table and a role in the filter", async () => {
    const { dialog } = await openDialog(
      `${PATH}?group=ops&role=ANALYST&environment=dev&namespace=sales&table=orders`,
    );

    expect(within(dialog).getByRole("radio", { name: "A group" })).toBeChecked();
    expect(within(dialog).getByRole("textbox", { name: "Table" })).toHaveValue("orders");
    expect(within(dialog).getByRole("button", { name: /Role$/ })).toHaveTextContent("ANALYST");
    expect(within(dialog).getByRole("button", { name: "Grant" })).toBeEnabled();
  });

  it("starts from an action the filter names", async () => {
    const { dialog } = await openDialog(`${PATH}?action=READ_TABLE&kind=INSTANCE`);

    expect(within(dialog).getByRole("button", { name: /Action$/ })).toHaveTextContent(
      "Read tables",
    );
    expect(within(dialog).getByRole("radio", { name: "The whole instance" })).toBeChecked();
  });

  it("sends nothing on Enter while the form is not complete", async () => {
    const { user, dialog, server } = await openDialog();

    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "maria{Enter}");

    expect(screen.getByRole("dialog", { name: "Add grant" })).toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "POST")).toBe(false);
  });

  it("says to add a passkey first when the account has none", async () => {
    const shell = await openDialog(
      PATH,
      grantRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        "POST /api/v1/sign-in/step-up/options": {
          status: 409,
          body: { type: "NoPasskeyError", detail: "Your account has no passkey at this address." },
        },
      }),
    );
    const { user, dialog } = shell;

    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "maria{Escape}");
    await user.click(within(dialog).getByRole("radio", { name: "The whole instance" }));
    await choose(shell, /Role$/, /^VIEWER/);
    await user.click(within(dialog).getByRole("button", { name: "Grant" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(/no passkey yet/);
    expect(within(alert).getByRole("link")).toHaveAttribute("href", "/account");
  });

  it("asks for the environment unless the grant is on the instance", async () => {
    const shell = await openDialog();
    const { user, dialog } = shell;
    const grant = within(dialog).getByRole("button", { name: "Grant" });

    expect(grant).toHaveAccessibleDescription("Name who the grant is for.");
    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "maria{Escape}");
    expect(grant).toHaveAccessibleDescription("Choose a role or an action.");
    await choose(shell, /Role$/, /^VIEWER/);
    expect(grant).toBeDisabled();
    expect(grant).toHaveAccessibleDescription("Choose the environment.");
    await user.click(within(dialog).getByRole("radio", { name: "A namespace" }));
    await choose(shell, /Environment$/, "prod");
    expect(grant).toHaveAccessibleDescription("Name the namespace.");
    await user.click(within(dialog).getByRole("radio", { name: "The whole instance" }));
    expect(grant).toBeEnabled();
  });

  it("does not offer actions that do nothing without the ADMIN role", async () => {
    const { user, dialog } = await openDialog();

    await user.click(within(dialog).getByRole("radio", { name: "A single action" }));
    await user.click(within(dialog).getByRole("button", { name: /Action$/ }));
    const options = (await screen.findAllByRole("option")).map((option) => option.textContent);
    expect(options).toHaveLength(23);
    for (const action of ["Manage grants", "Manage groups", "Manage API tokens"]) {
      expect(options).not.toContain(action);
    }
    expect(NO_EFFECT_ACTIONS).toEqual(["MANAGE_GRANT", "MANAGE_GROUP", "MANAGE_TOKEN"]);
  });

  it("refuses a managed environment", async () => {
    const shell = await openDialog(PATH, grantRoutes(accessStatusRoute(["prod"])));
    const { user, dialog } = shell;

    await user.click(within(dialog).getByRole("button", { name: /Environment$/ }));
    const prod = await screen.findByRole("option", { name: /prod/ });
    expect(prod).toHaveTextContent("prod ⌂ Git");
    expect(screen.getByRole("option", { name: /dev/ })).not.toHaveTextContent("Git");
    await user.click(prod);

    expect(within(dialog).queryByRole("button", { name: "Grant" })).toBeNull();
    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "maria{Escape}");
    await choose(shell, /Role$/, /^VIEWER/);
    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "{Enter}");
    expect(shell.server.sent.some((request) => request.method === "POST")).toBe(false);
    expect(dialog).toHaveTextContent(
      "An access file manages prod: give this grant in access.yaml instead.",
    );
    await choose(shell, /Environment$/, /dev/);
    expect(within(dialog).getByRole("button", { name: "Grant" })).toBeInTheDocument();
  });

  it("keeps every field with the server's sentence", async () => {
    const shell = await openDialog(
      PATH,
      grantRoutes({
        "POST /api/v1/grants": {
          status: 404,
          body: { detail: "No principal named 'mario'.", type: "NotFoundError" },
        },
      }),
    );
    const { user, dialog } = shell;

    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "mario{Escape}");
    await choose(shell, /Role$/, /^VIEWER/);
    await choose(shell, /Environment$/, "dev");
    await user.click(within(dialog).getByRole("button", { name: "Grant" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("No principal named 'mario'.");
    await waitFor(() => {
      expect(alert).toHaveFocus();
    });
    expect(within(dialog).getByRole("combobox", { name: "Name" })).toHaveValue("mario");
    expect(within(dialog).getByRole("button", { name: /Role$/ })).toHaveTextContent("VIEWER");
    expect(within(dialog).getByRole("button", { name: /Environment$/ })).toHaveTextContent("dev");
  });

  it("a cancelled touch grants nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const shell = await openDialog(
      PATH,
      grantRoutes({ "GET /api/v1/sign-in/session": STALE_SESSION, ...STEP_UP_ROUTES }),
    );
    const { user, dialog, server } = shell;

    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "maria{Escape}");
    await user.click(within(dialog).getByRole("radio", { name: "The whole instance" }));
    await choose(shell, /Role$/, /^VIEWER/);
    await user.click(within(dialog).getByRole("button", { name: "Grant" }));

    expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
    expect(
      server.sent.some((request) => request.method === "POST" && request.path === "/api/v1/grants"),
    ).toBe(false);
  });

  it("keeps the dialog open while granting", async () => {
    const answer = held();
    const shell = await openDialog(PATH, grantRoutes({ "POST /api/v1/grants": answer.route }));
    const { user, dialog } = shell;

    await user.type(within(dialog).getByRole("combobox", { name: "Name" }), "maria{Escape}");
    await user.click(within(dialog).getByRole("radio", { name: "The whole instance" }));
    await choose(shell, /Role$/, /^VIEWER/);
    await user.click(within(dialog).getByRole("button", { name: "Grant" }));
    await user.click(within(dialog).getByRole("combobox", { name: "Name" }));
    await user.keyboard("{Enter}");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Add grant" })).toBeInTheDocument();
    expect(
      shell.server.sent.filter(
        (request) => request.method === "POST" && request.path === "/api/v1/grants",
      ),
    ).toHaveLength(1);
    answer.release({ status: 201, body: aGrant() });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("Escape returns the focus to Add grant", async () => {
    const { user } = await openDialog();

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Add grant" })).toHaveFocus();
    });
    vi.restoreAllMocks();
  });
});
