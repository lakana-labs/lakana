import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { areasFor } from "../shell/areas";
import {
  anEnvironment,
  aSecret,
  freshSession,
  STALE_SESSION,
  STEP_UP_ROUTES,
} from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const PATH = "/settings/access/secrets";
const SENTINEL = "s3cr3t-sentinel";

/**
 * Renders Secrets for kim, who manages the secrets of dev.
 *
 * @param more - Routes that differ.
 * @returns The shell.
 */
function renderSecrets(more: Record<string, FakeRoute> = {}): ReturnType<typeof renderShell> {
  return renderShell(PATH, {
    access: areasFor(false),
    me: { name: "kim", admin: false },
    routes: {
      "GET /api/v1/whoami": { status: 200, body: whoami("kim", false, true, false, ["dev"]) },
      "GET /api/v1/environments": { status: 200, body: [anEnvironment("dev")] },
      "GET /api/v1/environments/dev/secrets": { status: 200, body: [aSecret()] },
      "GET /api/v1/sign-in/session": freshSession(),
      ...more,
    },
  });
}

/**
 * Opens Add secret.
 *
 * @param shell - The rendered shell.
 * @returns The dialog.
 */
async function openAdd(shell: ReturnType<typeof renderShell>): Promise<HTMLElement> {
  await screen.findByRole("region", { name: "Secrets of dev" });
  await shell.user.click(screen.getByRole("button", { name: "Add secret" }));
  return screen.findByRole("dialog", { name: "Add a secret to dev" });
}

/**
 * Chooses a use in a dialog.
 *
 * @param shell - The rendered shell.
 * @param dialog - The dialog.
 * @param option - The option's name.
 */
async function chooseUse(
  shell: ReturnType<typeof renderShell>,
  dialog: HTMLElement,
  option: string,
): Promise<void> {
  await shell.user.click(within(dialog).getByRole("button", { name: /Use$/ }));
  await shell.user.click(await screen.findByRole("option", { name: option }));
}

/**
 * The fields of the values, in order.
 *
 * @param dialog - The dialog.
 * @returns The key fields and the value fields.
 */
function valueFields(dialog: HTMLElement): { keys: HTMLElement[]; values: HTMLElement[] } {
  return {
    keys: within(dialog).getAllByRole("textbox", { name: "Key" }),
    values: within(dialog).getAllByLabelText("Value"),
  };
}

/**
 * Fills the pypi-corp form: name, Package index corp, user name u and the sentinel password.
 *
 * @param shell - The rendered shell.
 * @param dialog - The dialog.
 */
async function fillPypi(shell: ReturnType<typeof renderShell>, dialog: HTMLElement): Promise<void> {
  await shell.user.type(within(dialog).getByRole("textbox", { name: "Name" }), "pypi-corp");
  await chooseUse(shell, dialog, "Package index");
  await shell.user.type(
    within(dialog).getByRole("textbox", { name: "Name of the package index" }),
    "corp",
  );
  const { keys, values } = valueFields(dialog);
  expect(keys.map((field) => (field as HTMLInputElement).value)).toEqual(["username", "password"]);
  await shell.user.type(values[0] as HTMLElement, "u");
  await shell.user.type(values[1] as HTMLElement, SENTINEL);
}

/**
 * Says where the sentinel is outside the request bodies: the page's HTML as it is (text and
 * every attribute, an input's `value` attribute included), the address, the storages and the
 * console. A still-open input's live value is not in the HTML.
 *
 * @param spies - The console spies.
 * @returns The places found.
 */
function whereIsTheSentinel(
  spies: readonly { readonly mock: { readonly calls: readonly unknown[] } }[],
): string[] {
  const found: string[] = [];
  if (document.documentElement.outerHTML.includes(SENTINEL)) found.push("html");
  if (document.body.textContent.includes(SENTINEL)) found.push("text");
  for (const element of document.querySelectorAll("*")) {
    for (const attribute of element.attributes) {
      if (attribute.value.includes(SENTINEL)) found.push(`attribute ${attribute.name}`);
    }
  }
  if (window.location.href.includes(SENTINEL)) found.push("address");
  for (const storage of [window.localStorage, window.sessionStorage]) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index) ?? "";
      if (`${key}${storage.getItem(key) ?? ""}`.includes(SENTINEL)) found.push("storage");
    }
  }
  for (const spy of spies) {
    if (JSON.stringify(spy.mock.calls).includes(SENTINEL)) found.push("console");
  }
  return found;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SecretDialogs", () => {
  it("asks for the touch before writing a secret", async () => {
    fakeWebAuthn();
    const shell = renderSecrets({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
      "POST /api/v1/environments/dev/secrets": { status: 201, body: aSecret() },
    });
    const dialog = await openAdd(shell);
    await fillPypi(shell, dialog);

    await shell.user.click(within(dialog).getByRole("button", { name: "Add secret" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const start = shell.server.sent.findIndex(
      (request) => request.path === "/api/v1/sign-in/session",
    );
    expect(
      shell.server.sent
        .slice(start, start + 4)
        .map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "POST /api/v1/environments/dev/secrets",
    ]);
    expect(shell.server.sent[start + 3]?.body).toEqual({
      name: "pypi-corp",
      values: { username: "u", password: SENTINEL },
      use: { kind: "package_index", target: "corp" },
    });
  });

  it("a value never leaves its form", async () => {
    const spies = (["log", "info", "warn", "error"] as const).map((level) =>
      vi.spyOn(console, level),
    );
    let refuse = true;
    const shell = renderSecrets({
      "POST /api/v1/environments/dev/secrets": () =>
        refuse
          ? { status: 422, body: { detail: "The value of password is not valid text." } }
          : { status: 201, body: aSecret() },
    });
    const dialog = await openAdd(shell);
    await fillPypi(shell, dialog);

    await shell.user.click(within(dialog).getByRole("button", { name: "Add secret" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("not valid text");
    expect(valueFields(dialog).values[1]).toHaveValue(SENTINEL);
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("pypi-corp");
    expect(whereIsTheSentinel(spies)).toEqual([]);

    refuse = false;
    await shell.user.click(within(dialog).getByRole("button", { name: "Add secret" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(whereIsTheSentinel(spies)).toEqual([]);
    expect(document.body.innerHTML).not.toContain(SENTINEL);
    const carrying = shell.server.sent.filter((request) =>
      JSON.stringify(request).includes(SENTINEL),
    );
    expect(carrying.map((request) => request.method)).toEqual(["POST", "POST"]);
    for (const request of carrying) {
      expect(request.path).not.toContain(SENTINEL);
      expect(JSON.stringify(request.headers)).not.toContain(SENTINEL);
    }
  });

  it("after Cancel nothing holds a value", async () => {
    const shell = renderSecrets();
    const dialog = await openAdd(shell);
    await shell.user.type(valueFields(dialog).values[0] as HTMLElement, SENTINEL);
    const field = valueFields(dialog).values[0] as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(field).toHaveAttribute("autocomplete", "off");
    expect(field).not.toHaveAttribute("name");

    await shell.user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(document.body.innerHTML).not.toContain(SENTINEL);
    expect(shell.server.sent.some((request) => request.method === "POST")).toBe(false);
  });

  it("refuses empty or repeated keys before sending", async () => {
    const shell = renderSecrets();
    const dialog = await openAdd(shell);
    await shell.user.type(within(dialog).getByRole("textbox", { name: "Name" }), "git");
    await shell.user.click(within(dialog).getByRole("button", { name: "Add a value" }));
    const { keys } = valueFields(dialog);
    await shell.user.type(keys[0] as HTMLElement, "token");
    await shell.user.type(keys[1] as HTMLElement, "token");
    await shell.user.tab();

    expect(
      await within(dialog).findAllByText("Another value has the key token; give each its own."),
    ).toHaveLength(2);
    await shell.user.click(within(dialog).getByRole("button", { name: "Add secret" }));
    expect(
      within(dialog).getAllByText("Another value has the key token; give each its own."),
    ).toHaveLength(2);
    const [, again] = within(dialog).getAllByRole("button", { name: "Remove the value token" });
    await shell.user.click(again as HTMLElement);
    expect(within(dialog).queryByText(/Another value has the key/)).not.toBeInTheDocument();
    await shell.user.clear(valueFields(dialog).keys[0] as HTMLElement);
    expect(within(dialog).getByText("Name the key.")).toBeVisible();
    expect(shell.server.sent.some((request) => request.method === "POST")).toBe(false);
  });

  it("names a missing target before sending, and fills the repository's key", async () => {
    const shell = renderSecrets();
    const dialog = await openAdd(shell);
    await shell.user.type(within(dialog).getByRole("textbox", { name: "Name" }), "git");
    await shell.user.click(within(dialog).getByRole("button", { name: "Add a value" }));
    await shell.user.click(within(dialog).getByRole("button", { name: "Add a value" }));
    const [, , third] = within(dialog).getAllByRole("button", { name: "Remove this value" });
    await shell.user.click(third as HTMLElement);
    await chooseUse(shell, dialog, "Repository");
    expect(valueFields(dialog).keys.map((field) => (field as HTMLInputElement).value)).toEqual([
      "token",
      "",
    ]);
    await chooseUse(shell, dialog, "Repository");
    expect(valueFields(dialog).keys).toHaveLength(2);

    await shell.user.click(within(dialog).getByRole("button", { name: "Add secret" }));

    expect(within(dialog).getByText("Name the repository.")).toBeVisible();
    expect(within(dialog).getByText("Name the key.")).toBeVisible();
    await shell.user.type(
      within(dialog).getByRole("textbox", { name: "Name of the repository" }),
      "analytics",
    );
    await chooseUse(shell, dialog, "Connection");
    expect(valueFields(dialog).keys).toHaveLength(2);
    await chooseUse(shell, dialog, "None");
    expect(within(dialog).queryByRole("textbox", { name: /^Name of/ })).not.toBeInTheDocument();
    expect(shell.server.sent.some((request) => request.method === "POST")).toBe(false);
  });

  it("keeps the fields with the server's sentence", async () => {
    const shell = renderSecrets({
      "POST /api/v1/environments/dev/secrets": {
        status: 409,
        body: {
          detail: "A secret named 'pypi-corp' already exists in dev.",
          type: "AlreadyExistsError",
        },
      },
    });
    const dialog = await openAdd(shell);
    await fillPypi(shell, dialog);

    await shell.user.click(within(dialog).getByRole("button", { name: "Add secret" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("already exists");
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("pypi-corp");
    expect(within(dialog).getByRole("textbox", { name: "Name of the package index" })).toHaveValue(
      "corp",
    );
    expect(within(dialog).getByRole("button", { name: /Package index/ })).toBeVisible();
  });

  it("a cancelled touch writes nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const shell = renderSecrets({
      "GET /api/v1/sign-in/session": STALE_SESSION,
      ...STEP_UP_ROUTES,
    });
    const dialog = await openAdd(shell);
    await fillPypi(shell, dialog);

    await shell.user.click(within(dialog).getByRole("button", { name: "Add secret" }));

    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(
      shell.server.sent.some(
        (request) => request.method === "POST" && request.path.endsWith("/secrets"),
      ),
    ).toBe(false);
  });

  it("rotates and returns the focus to Rotate", async () => {
    let rotated = false;
    const shell = renderSecrets({
      "GET /api/v1/environments/dev/secrets": () => ({
        status: 200,
        body: [aSecret(rotated ? { rotated_at: "2026-11-18T10:00:00Z" } : {})],
      }),
      "PATCH /api/v1/environments/dev/secrets/pypi-corp": () => {
        rotated = true;
        return { status: 200, body: aSecret() };
      },
    });
    const rotate = await screen.findByRole("button", { name: "Rotate pypi-corp" });
    await shell.user.click(rotate);
    const dialog = await screen.findByRole("dialog", { name: "Rotate pypi-corp" });
    expect(dialog).toHaveTextContent("What uses it picks the new value up.");
    const { keys, values } = valueFields(dialog);
    await shell.user.type(keys[0] as HTMLElement, "token");
    await shell.user.type(values[0] as HTMLElement, "t");

    await shell.user.click(within(dialog).getByRole("button", { name: "Rotate" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Rotate pypi-corp" })).toHaveFocus();
    });
    const index = shell.server.sent.findIndex((request) => request.method === "PATCH");
    expect(shell.server.sent[index]?.body).toEqual({ values: { token: "t" } });
    expect(shell.server.sent[index - 1]?.path).toBe("/api/v1/sign-in/session");
    await waitFor(() => {
      expect(screen.queryByText("—")).not.toBeInTheDocument();
    });
  });

  it("rotates nothing with a key missing", async () => {
    const shell = renderSecrets();
    await shell.user.click(await screen.findByRole("button", { name: "Rotate pypi-corp" }));
    const dialog = await screen.findByRole("dialog", { name: "Rotate pypi-corp" });

    await shell.user.click(within(dialog).getByRole("button", { name: "Rotate" }));

    expect(within(dialog).getByText("Name the key.")).toBeVisible();
    expect(shell.server.sent.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("sets the use", async () => {
    const shell = renderSecrets({
      "PUT /api/v1/environments/dev/secrets/pypi-corp/use": { status: 200, body: aSecret() },
    });
    await shell.user.click(await screen.findByRole("button", { name: "Set use of pypi-corp" }));
    const dialog = await screen.findByRole("dialog", { name: "Set the use of pypi-corp" });
    expect(within(dialog).getByRole("textbox", { name: "Name of the package index" })).toHaveValue(
      "corp",
    );

    await chooseUse(shell, dialog, "This environment's storage and webhooks");
    expect(within(dialog).queryByRole("textbox", { name: /^Name of/ })).not.toBeInTheDocument();
    await shell.user.click(within(dialog).getByRole("button", { name: "Set use" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const put = shell.server.sent.find((request) => request.method === "PUT");
    expect(put?.body).toEqual({ kind: "environment", target: null });
  });

  it("asks for a use before setting one", async () => {
    const shell = renderSecrets({
      "GET /api/v1/environments/dev/secrets": {
        status: 200,
        body: [aSecret({ use: null, use_target: null })],
      },
    });
    await shell.user.click(await screen.findByRole("button", { name: "Set use of pypi-corp" }));
    const dialog = await screen.findByRole("dialog", { name: "Set the use of pypi-corp" });

    await shell.user.click(within(dialog).getByRole("button", { name: "Set use" }));

    expect(within(dialog).getByRole("alert")).toHaveTextContent("Choose a use.");
    await shell.user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(shell.server.sent.some((request) => request.method === "PUT")).toBe(false);
  });

  it("deletes after the name is typed, without a touch", async () => {
    let deleted = false;
    const shell = renderSecrets({
      "GET /api/v1/environments/dev/secrets": () => ({
        status: 200,
        body: deleted ? [] : [aSecret()],
      }),
      "DELETE /api/v1/environments/dev/secrets/pypi-corp": () => {
        deleted = true;
        return { status: 204 };
      },
    });
    await shell.user.click(await screen.findByRole("button", { name: "Delete pypi-corp" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete pypi-corp?" });
    expect(dialog).toHaveTextContent("Package index corp uses it, and stops working.");
    const submit = within(dialog).getByRole("button", { name: "Delete the secret" });
    expect(submit).toBeDisabled();

    await shell.user.type(
      within(dialog).getByRole("textbox", { name: "Type pypi-corp to delete it" }),
      "pypi-corp",
    );
    await shell.user.click(submit);

    expect(await screen.findByText("No secret in dev yet.")).toBeVisible();
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Secrets" })).toHaveFocus();
    });
    const index = shell.server.sent.findIndex((request) => request.method === "DELETE");
    expect(shell.server.sent[index - 1]?.path).not.toBe("/api/v1/sign-in/session");
  });

  it("sends one delete while the first is on its way", async () => {
    const request = held();
    const shell = renderSecrets({
      "DELETE /api/v1/environments/dev/secrets/pypi-corp": request.route,
    });
    await shell.user.click(await screen.findByRole("button", { name: "Delete pypi-corp" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete pypi-corp?" });

    await shell.user.type(within(dialog).getByRole("textbox"), "pypi-corp{Enter}{Enter}");
    request.release({ status: 204 });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(shell.server.sent.filter((sent) => sent.method === "DELETE")).toHaveLength(1);
  });

  it("a secret with no use is deleted without naming one", async () => {
    const shell = renderSecrets({
      "GET /api/v1/environments/dev/secrets": {
        status: 200,
        body: [aSecret({ use: null, use_target: null })],
      },
    });
    await shell.user.click(await screen.findByRole("button", { name: "Delete pypi-corp" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete pypi-corp?" });

    expect(dialog).not.toHaveTextContent("uses it");
    await shell.user.type(within(dialog).getByRole("textbox"), "pypi{Enter}");
    expect(shell.server.sent.some((request) => request.method === "DELETE")).toBe(false);
    await shell.user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Delete pypi-corp" })).toHaveFocus();
    });
  });
});
