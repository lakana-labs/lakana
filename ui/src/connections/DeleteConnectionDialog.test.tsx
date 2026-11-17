import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { aConnection, connectionRoutes } from "../test/connections";
import { held } from "../test/pending";
import { fakeServer } from "../test/server";
import { AreaScreen } from "./ConnectionsScreen";
import { DeleteConnectionDialog } from "./DeleteConnectionDialog";

const DELETE = "DELETE /api/v1/environments/prod/connections/crm";
const CRM = "GET /api/v1/environments/prod/connections/crm";

/**
 * The requests that change something, as `METHOD path`.
 *
 * @param sent - Every request.
 * @returns The writes.
 */
function writes(sent: readonly { method: string; path: string }[]): string[] {
  return sent
    .filter((request) => request.method !== "GET")
    .map((request) => `${request.method} ${request.path}`);
}

describe("DeleteConnectionDialog", () => {
  it("a declared connection's delete names its assets and sends nothing", async () => {
    const server = fakeServer({});
    const onDone = vi.fn();
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(
      <DeleteConnectionDialog
        connection={aConnection({
          declared_by: [
            { repository: "sales", asset: "orders" },
            { repository: "sales", asset: "customers" },
          ],
        })}
        onDone={onDone}
        onCancel={onCancel}
      />,
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("crm is still in use");
    expect(within(dialog).getByText("sales: orders")).toBeInTheDocument();
    expect(within(dialog).getByText("sales: customers")).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      "Remove it from the asset's @asset(connections=[...]) in git and deploy, then delete it.",
    );
    expect(dialog).toHaveTextContent("Its secret crm-password stays in the environment, unbound.");
    expect(within(dialog).queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onDone).not.toHaveBeenCalled();
    expect(server.sent).toEqual([]);
  });

  it("asks first, says the secret stays unbound, then deletes once and goes to the list with the note", async () => {
    const server = fakeServer({
      ...connectionRoutes([aConnection()], [{ name: "prod", actions: ["test"] }]),
      [CRM]: { status: 200, body: aConnection() },
      [DELETE]: { status: 204 },
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/connections/prod/crm"]}>
        <Routes>
          <Route path="/connections/*" element={<AreaScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Delete…" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAccessibleName("Delete the connection crm in prod?");
    expect(dialog).toHaveTextContent("Its secret crm-password stays in the environment, unbound.");
    expect(writes(server.sent)).toEqual([]);

    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    const note = await screen.findByText("Deleted crm.");
    expect(note).toHaveAttribute("role", "status");
    await waitFor(() => {
      expect(note).toHaveFocus();
    });
    expect(screen.getByRole("heading", { level: 1, name: "Connections" })).toBeInTheDocument();
    expect(writes(server.sent)).toEqual([DELETE]);
  });

  it("says that a connection has no secret", () => {
    fakeServer({});
    render(
      <DeleteConnectionDialog
        connection={aConnection({ secret: null })}
        onDone={() => undefined}
        onCancel={() => undefined}
      />,
    );

    expect(screen.getByRole("dialog")).toHaveTextContent("It has no secret.");
  });

  it("returns the focus to Delete… on Close", async () => {
    fakeServer({
      [CRM]: {
        status: 200,
        body: aConnection({ declared_by: [{ repository: "sales", asset: "orders" }] }),
      },
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/connections/prod/crm"]}>
        <Routes>
          <Route path="/connections/*" element={<AreaScreen />} />
        </Routes>
      </MemoryRouter>,
    );
    const open = await screen.findByRole("button", { name: "Delete…" });
    await user.click(open);

    await user.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(open).toHaveFocus();
    });
  });

  it("keeps the dialog with the server's sentence on a 409", async () => {
    fakeServer({
      [DELETE]: {
        status: 409,
        body: {
          type: "ConflictError",
          detail: "The connection crm is declared by sales: orders.",
        },
      },
    });
    const onDone = vi.fn();
    const user = userEvent.setup();
    render(
      <DeleteConnectionDialog
        connection={aConnection()}
        onDone={onDone}
        onCancel={() => undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The connection crm is declared by sales: orders.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("ignores Escape and Cancel while pending", async () => {
    const answer = held();
    const server = fakeServer({ [DELETE]: answer.route });
    const onDone = vi.fn();
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(
      <DeleteConnectionDialog connection={aConnection()} onDone={onDone} onCancel={onCancel} />,
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => {
      expect(writes(server.sent)).toEqual([DELETE]);
    });
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).not.toHaveBeenCalled();

    answer.release({ status: 204 });

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(writes(server.sent)).toEqual([DELETE]);
  });

  it("closes with Cancel and Escape when nothing is pending", async () => {
    const onCancel = vi.fn();
    fakeServer({});
    const user = userEvent.setup();
    render(
      <DeleteConnectionDialog
        connection={aConnection()}
        onDone={() => undefined}
        onCancel={onCancel}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.keyboard("{Escape}");

    expect(onCancel).toHaveBeenCalledTimes(2);
  });
});
