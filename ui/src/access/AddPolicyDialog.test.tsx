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
import { item } from "../test/catalog";
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
    "GET /api/v1/tables": { status: 200, body: { items: [], next_cursor: null, capped: false } },
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
  // Escape closes the list of suggestions and leaves the dialog open.
  await user.type(
    within(dialog).getByRole("combobox", { name: "Table" }),
    "station_ranking{Escape}",
  );
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
    await user.click(within(dialog).getByRole("radio", { name: "Person or service account" }));
    const masks = reads(server.sent, "column-masks");

    await user.click(create);

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    const post = server.sent.find(
      (request) => request.path === "/api/v1/column-masks" && request.method === "POST",
    );
    expect(post?.body).toMatchObject({
      column: "email",
      expression: "'***'",
      principal: "analysts",
      group: null,
    });
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
    expect(within(dialog).getByRole("combobox", { name: "Table" })).toHaveValue("station_ranking");
  });

  it("suggests the tables of the chosen environment and fills in the namespace of the chosen one", async () => {
    const shell = await openDialog(
      PATH,
      policyRoutes({
        "GET /api/v1/tables": {
          status: 200,
          body: {
            items: [
              item({ environment: "dev", namespace: "gold", name: "station_ranking" }),
              item({ environment: "dev", namespace: "silver.weather", name: "station_daily" }),
            ],
            next_cursor: null,
            capped: false,
          },
        },
      }),
    );
    const { user, dialog, server } = shell;
    const table = within(dialog).getByRole("combobox", { name: "Table" });
    expect(table).toHaveAccessibleDescription("Choose the environment first to see its tables.");
    await user.type(table, "sta");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(server.sent.some((request) => request.path.startsWith("/api/v1/tables"))).toBe(false);
    await user.clear(table);

    await chooseEnvironment(shell, "dev");
    await user.type(table, "station");
    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "station_rankinggold",
      "station_dailysilver.weather",
    ]);
    await user.click(options[1] as HTMLElement);

    expect(table).toHaveValue("station_daily");
    expect(within(dialog).getByRole("textbox", { name: "Namespace" })).toHaveValue(
      "silver.weather",
    );
    expect(
      server.sent.filter((request) => request.path.startsWith("/api/v1/tables")).map((r) => r.path),
    ).toEqual(["/api/v1/tables?environment=dev&sort=name&limit=20&name=station"]);
    expect(screen.getByRole("dialog", { name: "Add filter or mask" })).toBeInTheDocument();
  });

  it("offers no table of the environment chosen before", async () => {
    const answer = held();
    const shell = await openDialog(
      PATH,
      policyRoutes({
        "GET /api/v1/tables": (request) =>
          request.path.includes("environment=dev")
            ? {
                status: 200,
                body: {
                  items: [item({ environment: "dev", namespace: "gold", name: "station_ranking" })],
                  next_cursor: null,
                  capped: false,
                },
              }
            : answer.route(),
      }),
    );
    const { user, dialog } = shell;
    const table = within(dialog).getByRole("combobox", { name: "Table" });
    await chooseEnvironment(shell, "dev");
    await user.type(table, "station");
    expect(await screen.findByRole("option")).toHaveTextContent("station_ranking");
    await user.keyboard("{Escape}");

    await chooseEnvironment(shell, "prod");
    await user.click(table);
    await user.keyboard("{ArrowDown}");

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Looking for names…");
    });
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("says when no table of that name may be read there", async () => {
    const shell = await openDialog();
    const { user, dialog } = shell;
    await chooseEnvironment(shell, "prod");

    await user.type(within(dialog).getByRole("combobox", { name: "Table" }), "secret_salaries");

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "No table named “secret_salaries” that you may read in prod.",
      );
    });
    expect(screen.queryAllByRole("option")).toEqual([]);
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
    await user.type(within(dialog).getByRole("combobox", { name: "Table" }), "t{Escape}");
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
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "maria");
    await user.type(within(dialog).getByRole("textbox", { name: "Namespace" }), "gold");
    await user.type(within(dialog).getByRole("combobox", { name: "Table" }), "t{Enter}");
    expect(shell.server.sent.some((request) => request.method === "POST")).toBe(false);
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
    expect(within(dialog).getByRole("combobox", { name: "Table" })).toHaveValue("station_ranking");
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
    await user.type(within(dialog).getByRole("combobox", { name: "Table" }), "{Enter}");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Add filter or mask" })).toBeInTheDocument();
    expect(
      shell.server.sent.filter(
        (request) => request.method === "POST" && request.path === "/api/v1/row-filters",
      ),
    ).toHaveLength(1);
    answer.release({ status: 201, body: aRowFilter() });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("takes a typed environment from a person who may list none", async () => {
    const shell = await openDialog(
      PATH,
      policyRoutes({
        "GET /api/v1/environments": { status: 200, body: [] },
        "POST /api/v1/row-filters": { status: 201, body: aRowFilter() },
      }),
    );
    const { user, dialog, server } = shell;
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "maria");
    const environment = within(dialog).getByRole("textbox", { name: "Environment" });
    await user.type(environment, " dev ");
    await user.type(within(dialog).getByRole("textbox", { name: "Namespace" }), "gold");
    await user.type(within(dialog).getByRole("combobox", { name: "Table" }), "t");
    await user.type(within(dialog).getByRole("textbox", { name: "Expression" }), "TRUE");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    const post = server.sent.find((request) => request.method === "POST");
    expect(post?.body).toMatchObject({ environment: "dev", principal: "maria" });
  });

  it("refuses a typed managed environment, spaces and all", async () => {
    const { user, dialog } = await openDialog(
      PATH,
      policyRoutes({
        ...managedScopesRoute(["prod"], "team.yaml"),
        "GET /api/v1/environments": { status: 200, body: [] },
      }),
    );

    await user.type(within(dialog).getByRole("textbox", { name: "Environment" }), "prod ");

    expect(within(dialog).queryByRole("button", { name: "Create" })).toBeNull();
    expect(dialog).toHaveTextContent("An access file manages prod: add this in team.yaml instead.");
  });

  it("offers the choice while the environments are read", async () => {
    const environments = held();
    const { dialog } = await openDialog(
      PATH,
      policyRoutes({ "GET /api/v1/environments": environments.route }),
    );

    expect(within(dialog).getByRole("button", { name: /Environment$/ })).toBeInTheDocument();
    environments.release({ status: 200, body: [] });
    expect(await within(dialog).findByRole("textbox", { name: "Environment" })).toHaveValue("");
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
