import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { manageRoutes } from "../../test/manage";
import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { NewNamespaceDialog } from "./NewNamespaceDialog";

const CREATE = "POST /api/v1/environments/prod/namespaces";
const PROTECTED = {
  "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
};

/**
 * Renders the dialog in `prod`, under a namespace or the environment itself.
 *
 * @param parent - The namespace it opens under; null for the environment.
 * @returns The handlers and a user-event session.
 */
function renderCreate(parent: string | null = "gold") {
  const onDone = vi.fn();
  const onCancel = vi.fn();
  const user = userEvent.setup();
  render(
    <NewNamespaceDialog environment="prod" parent={parent} onDone={onDone} onCancel={onCancel} />,
  );
  return { onDone, onCancel, user };
}

/**
 * The bodies of the requests that create a namespace.
 *
 * @param sent - Every request.
 * @returns The bodies, in order.
 */
function creations(sent: readonly { method: string; body: unknown }[]): unknown[] {
  return sent.filter((request) => request.method === "POST").map((request) => request.body);
}

describe("NewNamespaceDialog", () => {
  it("starts with the parent and a dot under a namespace, and empty under an environment", async () => {
    fakeServer(manageRoutes());
    const { unmount } = render(
      <NewNamespaceDialog environment="prod" parent="gold" onDone={vi.fn()} onCancel={vi.fn()} />,
    );

    expect(screen.getByRole("dialog")).toHaveAccessibleName("New namespace in gold");
    const field = screen.getByRole("textbox", { name: "Path" });
    expect(field).toHaveValue("gold.");
    expect(field).toHaveAccessibleDescription("Dotted, such as gold.sales.");
    expect(field.tagName).toBe("INPUT");
    await screen.findByRole("button", { name: "Create" });
    unmount();

    renderCreate(null);
    expect(screen.getByRole("dialog")).toHaveAccessibleName("New namespace in prod");
    expect(screen.getByRole("textbox", { name: "Path" })).toHaveValue("");
  });

  it("sends the trimmed path and calls onDone with it", async () => {
    const server = fakeServer(manageRoutes());
    const { onDone, user } = renderCreate();

    await user.type(screen.getByRole("textbox", { name: "Path" }), "finance  ");
    await user.click(await screen.findByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledWith("gold.finance");
    });
    expect(creations(server.sent)).toEqual([
      { path: "gold.finance", properties: {}, break_glass_reason: null },
    ]);
  });

  it("sends on Enter in the path", async () => {
    const server = fakeServer(manageRoutes());
    const { onDone, user } = renderCreate(null);

    await screen.findByRole("button", { name: "Create" });
    await user.type(screen.getByRole("textbox", { name: "Path" }), "platinum{Enter}");

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledWith("platinum");
    });
    expect(creations(server.sent)).toHaveLength(1);
  });

  it("shows the server's 409 for an existing namespace word for word and keeps the path", async () => {
    const detail = "Namespace gold.sales already exists in 'prod'.";
    fakeServer({
      ...manageRoutes(),
      [CREATE]: { status: 409, body: { type: "AlreadyExistsError", detail } },
    });
    const { onDone, user } = renderCreate();

    await user.type(screen.getByRole("textbox", { name: "Path" }), "sales");
    await user.click(await screen.findByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("textbox", { name: "Path" })).toHaveValue("gold.sales");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("ignores Escape and Cancel while the request is pending", async () => {
    const slow = held();
    fakeServer({ ...manageRoutes(), [CREATE]: slow.route });
    const { onDone, onCancel, user } = renderCreate();

    await user.type(screen.getByRole("textbox", { name: "Path" }), "finance");
    await user.click(await screen.findByRole("button", { name: "Create" }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    slow.release({ status: 201, body: { path: "gold.finance", properties: {} } });
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledWith("gold.finance");
    });
  });

  it("closes on Escape and Cancel when nothing is pending", async () => {
    fakeServer(manageRoutes());
    const { onCancel, user } = renderCreate();

    await screen.findByRole("button", { name: "Create" });
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it("requires a reason in a protected environment and sends it in the body", async () => {
    const server = fakeServer({ ...manageRoutes(), ...PROTECTED });
    const { onDone, user } = renderCreate();

    await user.type(screen.getByRole("textbox", { name: "Path" }), "finance");
    const reason = await screen.findByRole("textbox", {
      name: /Why you break the protection of prod/,
    });
    const create = screen.getByRole("button", { name: "Create" });
    expect(create).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Path" }), "{Enter}");
    expect(creations(server.sent)).toEqual([]);

    await user.type(reason, "Quarter close needs it");
    await user.click(create);

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledWith("gold.finance");
    });
    expect(creations(server.sent)).toEqual([
      { path: "gold.finance", properties: {}, break_glass_reason: "Quarter close needs it" },
    ]);
  });

  it("sends a pasted reason as one line", async () => {
    const server = fakeServer({ ...manageRoutes(), ...PROTECTED });
    const { onDone, user } = renderCreate();

    await user.type(screen.getByRole("textbox", { name: "Path" }), "finance");
    await user.click(await screen.findByRole("textbox", { name: /Why you break/ }));
    await user.paste("fix\nnow please");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    const [body] = creations(server.sent) as { break_glass_reason: string }[];
    expect(body?.break_glass_reason).not.toMatch(/[\n\r]/);
    expect(body?.break_glass_reason).toMatch(/^fix ?now please$/);
  });
});
