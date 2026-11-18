import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { manageRoutes } from "../../test/manage";
import { rawRoutes } from "../../test/raw";
import { held } from "../../test/pending";
import { type FakeRoute, fakeServer } from "../../test/server";
import ExternalTableDialog from "./ExternalTableDialog";

const REGISTER = "POST /api/v1/environments/prod/namespaces/bronze/tables/external";
const PROTECTED = {
  "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
};

/**
 * Renders the dialog for namespace `bronze` of `prod`, on the Catalog's page of that namespace,
 * in a router whose location a test can read.
 *
 * @param routes - The routes the fake server answers.
 * @returns The router, the handlers and a user-event session.
 */
function renderRegister(routes: Record<string, FakeRoute> = manageRoutes()) {
  const server = fakeServer(routes);
  const onCancel = vi.fn();
  // The menu goes to the table while its place is still shown (PlaceActions' tests cover the rest).
  const onDone = vi.fn((go: () => void) => {
    go();
  });
  const router = createMemoryRouter(
    [
      {
        path: "/catalog/prod/bronze",
        element: (
          <ExternalTableDialog
            environment="prod"
            namespace="bronze"
            onCancel={onCancel}
            onDone={onDone}
          />
        ),
      },
      { path: "/catalog/:environment/:namespace/:name", element: <p>The table</p> },
    ],
    { initialEntries: ["/catalog/prod/bronze"] },
  );
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);
  return { server, router, onCancel, onDone, user };
}

/**
 * The bodies of the requests that register a table.
 *
 * @param sent - Every request.
 * @returns The bodies, in order.
 */
function registrations(sent: readonly { method: string; body: unknown }[]): unknown[] {
  return sent.filter((request) => request.method === "POST").map((request) => request.body);
}

/**
 * Fills the name and puts a location after the `raw/` the field starts with.
 *
 * @param user - The user-event session.
 * @param name - The table's name.
 * @param rest - What follows `raw/`.
 */
async function fill(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
  rest: string,
): Promise<void> {
  await user.type(screen.getByRole("textbox", { name: "Name" }), name);
  await user.type(screen.getByRole("textbox", { name: "Location" }), rest);
}

describe("ExternalTableDialog", () => {
  it("starts with raw/ and follows the location's format until one is picked", async () => {
    const { user } = renderRegister();

    expect(await screen.findByRole("dialog")).toHaveAccessibleName("New external table in bronze");
    const name = screen.getByRole("textbox", { name: "Name" });
    expect(name).toHaveAccessibleDescription("The table's name, without its namespace.");
    const location = screen.getByRole("textbox", { name: "Location" });
    expect(location).toHaveValue("raw/");
    expect(location).toHaveAccessibleDescription(
      "Files under raw/: a directory and a file pattern, such as raw/orders/*.csv. The table reads the matching files directly in that directory, and no other external table may use it. At least one file must match: Lakana reads one to learn the columns.",
    );
    for (const field of [name, location]) {
      expect(field.tagName).toBe("INPUT");
    }
    const format = screen.getByRole("radiogroup", { name: "Format" });
    expect(format).toHaveAttribute("aria-orientation", "horizontal");
    expect(within(format).getAllByRole("radio")).toEqual(
      ["CSV", "JSON", "Parquet"].map((label) => within(format).getByRole("radio", { name: label })),
    );
    expect(within(format).queryByRole("radio", { checked: true })).not.toBeInTheDocument();

    await user.type(location, "orders/*.CSV");
    expect(screen.getByRole("radio", { name: "CSV" })).toBeChecked();
    await user.clear(location);
    await user.type(location, "raw/orders/*.jsonl");
    expect(screen.getByRole("radio", { name: "JSON" })).toBeChecked();
    await user.type(location, "x");
    expect(within(format).queryByRole("radio", { checked: true })).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Parquet" }));
    await user.clear(location);
    await user.type(location, "raw/orders/*.csv");
    expect(screen.getByRole("radio", { name: "Parquet" })).toBeChecked();
  });

  it("fills the location from Browse… and lets the format follow it again", async () => {
    const { user } = renderRegister({ ...manageRoutes(), ...rawRoutes() });
    const browse = await screen.findByRole("button", { name: "Browse…" });
    const location = screen.getByRole("textbox", { name: "Location" });

    await user.click(screen.getByRole("radio", { name: "Parquet" }));
    expect(browse).toHaveAttribute("aria-expanded", "false");
    await user.click(browse);
    expect(browse).toHaveAttribute("aria-expanded", "true");
    const panel = await screen.findByRole("region", { name: "Files under raw/" });
    await user.click(await within(panel).findByRole("button", { name: "feeds/" }));
    await user.click(await within(panel).findByRole("button", { name: "2026-11-02.csv" }));

    expect(location).toHaveValue("raw/feeds/*.csv");
    expect(screen.getByRole("radio", { name: "CSV" })).toBeChecked();
    expect(screen.queryByRole("region", { name: "Files under raw/" })).not.toBeInTheDocument();
    expect(browse).toHaveAttribute("aria-expanded", "false");
    expect(location).toHaveFocus();

    await user.click(browse);
    expect(await screen.findByRole("button", { name: "feeds/" })).toBeInTheDocument();
    await user.click(browse);
    expect(screen.queryByRole("region", { name: "Files under raw/" })).not.toBeInTheDocument();
  });

  it("keeps Register disabled until a name, a location and a format are known", async () => {
    const { server, user } = renderRegister();
    const register = await screen.findByRole("button", { name: "Register" });

    expect(register).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Name" }), "orders");
    expect(register).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Location" }), "orders/*");
    expect(register).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Location" }), "{Enter}");
    expect(registrations(server.sent)).toEqual([]);

    await user.click(screen.getByRole("radio", { name: "CSV" }));
    expect(register).toBeEnabled();
    await user.clear(screen.getByRole("textbox", { name: "Name" }));
    expect(register).toBeDisabled();
    await user.clear(screen.getByRole("textbox", { name: "Location" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "orders");
    expect(register).toBeDisabled();
  });

  it("registers and goes to the new table's page", async () => {
    const { server, router, onDone, user } = renderRegister();

    await screen.findByRole("button", { name: "Register" });
    await fill(user, "orders", "orders/*.csv{Enter}");

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/catalog/prod/bronze/orders");
    });
    expect(registrations(server.sent)).toEqual([
      {
        name: "orders",
        location_pattern: "raw/orders/*.csv",
        format: "csv",
        break_glass_reason: null,
      },
    ]);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("sends the name and the location as typed, as the CLI does", async () => {
    const { server, user } = renderRegister();

    await screen.findByRole("button", { name: "Register" });
    await fill(user, "orders ", "orders/*.csv ");
    await user.click(screen.getByRole("radio", { name: "CSV" }));
    await user.click(screen.getByRole("button", { name: "Register" }));

    await waitFor(() => {
      expect(registrations(server.sent)).toHaveLength(1);
    });
    expect(registrations(server.sent)).toEqual([
      {
        name: "orders ",
        location_pattern: "raw/orders/*.csv ",
        format: "csv",
        break_glass_reason: null,
      },
    ]);
  });

  it("shows the server's location refusal word for word and keeps the fields", async () => {
    const detail = "Location pattern 'raw/orders/**/*.csv' must not contain '**'.";
    const { router, user } = renderRegister({
      ...manageRoutes(),
      [REGISTER]: { status: 422, body: { type: "ValidationError", detail } },
    });

    await screen.findByRole("button", { name: "Register" });
    await fill(user, "orders", "orders/**/*.csv");
    await user.click(screen.getByRole("button", { name: "Register" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    const dialog = screen.getByRole("dialog");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("orders");
    expect(screen.getByRole("textbox", { name: "Location" })).toHaveValue("raw/orders/**/*.csv");
    expect(screen.getByRole("radio", { name: "CSV" })).toBeChecked();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    expect(router.state.location.pathname).toBe("/catalog/prod/bronze");
  });

  it("shows that no file matches and keeps the dialog open", async () => {
    const detail = "No file matches 'raw/x/*.csv'.";
    const { router, onCancel, user } = renderRegister({
      ...manageRoutes(),
      [REGISTER]: { status: 404, body: { type: "NotFoundError", detail } },
    });

    await screen.findByRole("button", { name: "Register" });
    await fill(user, "orders", "x/*.csv{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("dialog")).toContainElement(document.activeElement as HTMLElement);
    expect(screen.getByRole("textbox", { name: "Location" })).toHaveValue("raw/x/*.csv");
    expect(onCancel).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe("/catalog/prod/bronze");
  });

  it("shows the git-owned name refusal word for word", async () => {
    const detail =
      "`bronze.orders` is declared by a deployed project. Defined in analytics/assets/bronze/orders.sql. Choose another name, or remove it there and deploy first.";
    const { router, user } = renderRegister({
      ...manageRoutes(),
      [REGISTER]: { status: 409, body: { type: "ConflictError", detail } },
    });

    await screen.findByRole("button", { name: "Register" });
    await fill(user, "orders", "orders/*.csv{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveValue("orders");
    expect(router.state.location.pathname).toBe("/catalog/prod/bronze");
  });

  it("requires a reason in a protected environment and sends it in the body", async () => {
    const { server, router, user } = renderRegister({ ...manageRoutes(), ...PROTECTED });

    await fill(user, "orders", "orders/*.csv");
    const reason = await screen.findByRole("textbox", {
      name: /Why you break the protection of prod/,
    });
    const register = screen.getByRole("button", { name: "Register" });
    expect(register).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Name" }), "{Enter}");
    expect(registrations(server.sent)).toEqual([]);

    await user.type(reason, "Quarter close needs the orders{Enter}");

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/catalog/prod/bronze/orders");
    });
    expect(registrations(server.sent)).toEqual([
      {
        name: "orders",
        location_pattern: "raw/orders/*.csv",
        format: "csv",
        break_glass_reason: "Quarter close needs the orders",
      },
    ]);
  });

  it("ignores Escape and Cancel while the request is pending", async () => {
    const slow = held();
    const { router, onCancel, user } = renderRegister({
      ...manageRoutes(),
      [REGISTER]: slow.route,
    });

    await screen.findByRole("button", { name: "Register" });
    await fill(user, "orders", "orders/*.parquet");
    await user.click(screen.getByRole("button", { name: "Register" }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    slow.release({
      status: 201,
      body: {
        name: "orders",
        namespace: "bronze",
        location_pattern: "raw/orders/*.parquet",
        format: "parquet",
        schema: {},
      },
    });
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/catalog/prod/bronze/orders");
    });
  });

  it("closes on Escape and Cancel when nothing is pending", async () => {
    const { onCancel, user } = renderRegister();

    await screen.findByRole("button", { name: "Register" });
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(2);
  });
});
