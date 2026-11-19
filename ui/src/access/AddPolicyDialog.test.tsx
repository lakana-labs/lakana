import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  aColumnMask,
  anEnvironment,
  aRowFilter,
  freshSession,
  managedScopesRoute,
  STALE_SESSION,
  STEP_UP_ROUTES,
} from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const PATH = "/settings/access/filters-and-masks";
const TOO_LONG =
  "The row-filter expression is longer than this server takes (4,000 characters). Shorten it.";

/**
 * The routes of Row filters & masks: dev and prod, nothing managed, no policy.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function policyRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...managedScopesRoute(),
    "GET /api/v1/sign-in/session": freshSession(),
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), anEnvironment("prod")],
    },
    "GET /api/v1/row-filters": { status: 200, body: [] },
    "GET /api/v1/column-masks": { status: 200, body: [] },
    ...more,
  };
}

/**
 * Opens Add filter or mask.
 *
 * @param path - The address, with the lists' filter.
 * @param routes - The routes.
 * @returns The dialog and the rendered shell.
 */
async function openDialog(path = PATH, routes = policyRoutes()) {
  const shell = renderShell(path, { routes });
  await shell.user.click(await screen.findByRole("button", { name: "Add filter or mask" }));
  const dialog = await screen.findByRole("dialog", { name: "Add filter or mask" });
  return { ...shell, dialog };
}

/** The opened dialog and its shell. */
type Opened = Awaited<ReturnType<typeof openDialog>>;

/**
 * Chooses the environment.
 *
 * @param shell - The opened dialog.
 * @param name - The environment.
 */
async function chooseEnvironment(shell: Opened, name: RegExp | string): Promise<void> {
  await shell.user.click(within(shell.dialog).getByRole("button", { name: /Environment$/ }));
  await shell.user.click(await screen.findByRole("option", { name }));
}

/**
 * Fills a row filter for the group analysts on dev › gold › station_ranking.
 *
 * @param shell - The opened dialog.
 * @param expression - The expression typed.
 */
async function fillFilter(shell: Opened, expression = "region = 'EU'"): Promise<void> {
  const { user, dialog } = shell;
  await user.click(within(dialog).getByRole("radio", { name: "Group" }));
  await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "analysts");
  await chooseEnvironment(shell, "dev");
  await user.type(within(dialog).getByRole("textbox", { name: "Namespace" }), "gold");
  await user.type(within(dialog).getByRole("textbox", { name: "Table" }), "station_ranking");
  await user.click(within(dialog).getByRole("textbox", { name: "Expression" }));
  await user.paste(expression);
}

/**
 * The requests to one list.
 *
 * @param sent - The requests.
 * @param list - `row-filters` or `column-masks`.
 * @returns How many reads of it were sent.
 */
function reads(sent: readonly { method: string; path: string }[], list: string): number {
  return sent.filter(
    (request) => request.method === "GET" && request.path.startsWith(`/api/v1/${list}`),
  ).length;
}

describe("AddPolicyDialog", () => {
  it("creates a row filter for a group", async () => {
    fakeWebAuthn();
    const shell = await openDialog(
      PATH,
      policyRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "POST /api/v1/row-filters": { status: 201, body: aRowFilter() },
      }),
    );
    await fillFilter(shell);
    const { user, dialog, server } = shell;
    const [filters, masks] = [
      reads(server.sent, "row-filters"),
      reads(server.sent, "column-masks"),
    ];
    const from = server.sent.length;

    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(server.sent[from]?.path).toBe("/api/v1/sign-in/session");
    const post = server.sent.find(
      (request) => request.method === "POST" && request.path === "/api/v1/row-filters",
    );
    expect(post?.body).toEqual({
      principal: null,
      group: "analysts",
      environment: "dev",
      namespace: "gold",
      table: "station_ranking",
      expression: "region = 'EU'",
    });
    expect(screen.getByText("Row filter created.")).toBeInTheDocument();
    await waitFor(() => {
      expect(reads(server.sent, "row-filters")).toBe(filters + 1);
    });
    expect(reads(server.sent, "column-masks")).toBe(masks);
  });

  it("creates a column mask with its column", async () => {
    const shell = await openDialog(
      PATH,
      policyRoutes({ "POST /api/v1/column-masks": { status: 201, body: aColumnMask() } }),
    );
    const { user, dialog, server } = shell;
    await user.click(within(dialog).getByRole("radio", { name: "Column mask" }));
    expect(within(dialog).getByText(/What they see instead/)).toBeInTheDocument();
    await fillFilter(shell, "'***'");
    const create = within(dialog).getByRole("button", { name: "Create" });
    expect(create).toHaveAccessibleDescription("Name the column.");
    await user.type(within(dialog).getByRole("textbox", { name: "Column" }), "email");
    const masks = reads(server.sent, "column-masks");

    await user.click(create);

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    const post = server.sent.find(
      (request) => request.path === "/api/v1/column-masks" && request.method === "POST",
    );
    expect(post?.body).toMatchObject({ column: "email", expression: "'***'", group: "analysts" });
    expect(screen.getByText("Column mask created.")).toBeInTheDocument();
    await waitFor(() => {
      expect(reads(server.sent, "column-masks")).toBe(masks + 1);
    });
  });

  it("starts from the list's filter", async () => {
    const { dialog } = await openDialog(
      `${PATH}?principal=maria&environment=dev&table=gold.station_ranking`,
    );

    expect(within(dialog).getByRole("radio", { name: "Person or service account" })).toBeChecked();
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toHaveValue("maria");
    expect(within(dialog).getByRole("button", { name: /Environment$/ })).toHaveTextContent("dev");
    expect(within(dialog).getByRole("textbox", { name: "Namespace" })).toHaveValue("gold");
    expect(within(dialog).getByRole("textbox", { name: "Table" })).toHaveValue("station_ranking");
  });

  it("asks for every field", async () => {
    const shell = await openDialog();
    const { user, dialog, server } = shell;
    const create = within(dialog).getByRole("button", { name: "Create" });
    expect(create).toHaveAccessibleDescription("Name who it is for.");
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "maria");
    expect(create).toHaveAccessibleDescription("Choose the environment.");
    await chooseEnvironment(shell, "dev");
    expect(create).toHaveAccessibleDescription("Name the namespace.");
    await user.type(within(dialog).getByRole("textbox", { name: "Namespace" }), "gold");
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription("Name the table.");
    await user.type(within(dialog).getByRole("textbox", { name: "Table" }), "t");
    expect(create).toHaveAccessibleDescription("Write the expression.");
    await user.type(within(dialog).getByRole("textbox", { name: "Namespace" }), "{Enter}");

    expect(server.sent.some((request) => request.method === "POST")).toBe(false);
  });

  it("refuses a managed environment", async () => {
    const shell = await openDialog(PATH, policyRoutes(managedScopesRoute(["prod"], "team.yaml")));
    const { user, dialog } = shell;

    await user.click(within(dialog).getByRole("button", { name: /Environment$/ }));
    expect(await screen.findByRole("option", { name: /prod/ })).toHaveTextContent("prod ⌂ Git");
    expect(screen.getByRole("option", { name: /dev/ })).not.toHaveTextContent("Git");
    await user.click(screen.getByRole("option", { name: /prod/ }));

    expect(within(dialog).queryByRole("button", { name: "Create" })).toBeNull();
    expect(dialog).toHaveTextContent("An access file manages prod: add this in team.yaml instead.");
  });

  it("keeps every field with the server's sentence", async () => {
    const long = "x".repeat(4100);
    const shell = await openDialog(
      PATH,
      policyRoutes({
        "POST /api/v1/row-filters": {
          status: 422,
          body: { detail: TOO_LONG, type: "ValidationError" },
        },
      }),
    );
    await fillFilter(shell, long);
    const { user, dialog } = shell;

    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(TOO_LONG);
    await waitFor(() => {
      expect(alert).toHaveFocus();
    });
    expect(within(dialog).getByRole("textbox", { name: "Expression" })).toHaveValue(long);
    expect(within(dialog).getByRole("textbox", { name: "Table" })).toHaveValue("station_ranking");
  });

  it("a cancelled touch creates nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const shell = await openDialog(
      PATH,
      policyRoutes({ "GET /api/v1/sign-in/session": STALE_SESSION, ...STEP_UP_ROUTES }),
    );
    await fillFilter(shell);

    await shell.user.click(within(shell.dialog).getByRole("button", { name: "Create" }));

    expect(await within(shell.dialog).findByRole("alert")).toBeVisible();
    expect(
      shell.server.sent.some(
        (request) => request.method === "POST" && request.path.includes("row-filters"),
      ),
    ).toBe(false);
  });

  it("says to add a passkey first when the account has none", async () => {
    const shell = await openDialog(
      PATH,
      policyRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        "POST /api/v1/sign-in/step-up/options": {
          status: 409,
          body: { type: "NoPasskeyError", detail: "Your account has no passkey at this address." },
        },
      }),
    );
    await fillFilter(shell);

    await shell.user.click(within(shell.dialog).getByRole("button", { name: "Create" }));

    const alert = await within(shell.dialog).findByRole("alert");
    expect(within(alert).getByRole("link")).toHaveAttribute("href", "/account");
  });

  it("keeps the dialog open while creating", async () => {
    const answer = held();
    const shell = await openDialog(
      PATH,
      policyRoutes({ "POST /api/v1/row-filters": answer.route }),
    );
    await fillFilter(shell);
    const { user, dialog } = shell;

    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Add filter or mask" })).toBeInTheDocument();
    answer.release({ status: 201, body: aRowFilter() });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("Escape returns the focus to Add filter or mask", async () => {
    const { user } = await openDialog();

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Add filter or mask" })).toHaveFocus();
    });
    vi.restoreAllMocks();
  });
});
