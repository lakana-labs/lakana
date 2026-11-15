import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { dropped } from "../../test/manage";
import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { TABLE } from "../../test/table";
import { UndropDialog } from "./UndropDialog";

const UNDROP = "POST /api/v1/environments/prod/namespaces/gold.sales/tables/fact_returns/undrop";
const TAKEN = "A table fact_returns already exists in gold.sales. Choose another name.";

describe("UndropDialog", () => {
  it("asks for no reason when the environment cannot be read", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod": { status: 403, body: { detail: "No." } },
      [UNDROP]: { status: 200, body: TABLE },
    });
    const onDone = vi.fn();
    const user = userEvent.setup();
    render(
      <UndropDialog
        row={dropped()}
        environment="prod"
        onDone={onDone}
        onCancel={vi.fn()}
        nameTaken={TAKEN}
      />,
    );

    await waitFor(() => {
      expect(server.sent.map((request) => request.path)).toContain("/api/v1/environments/prod");
    });
    expect(screen.getByRole("alert")).toHaveTextContent(TAKEN);
    expect(screen.queryByLabelText(/Why you break the protection/)).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Restore as" }), "fact_returns_2");
    await user.click(await screen.findByRole("button", { name: "Undrop" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledWith(TABLE);
    });
    expect(server.sent.at(-1)?.body).toEqual({
      new_name: "fact_returns_2",
      break_glass_reason: null,
    });
  });

  it("requires a reason in a protected environment and sends it as break_glass_reason", async () => {
    const protection = held();
    const server = fakeServer({
      "GET /api/v1/environments/prod": protection.route,
      [UNDROP]: { status: 200, body: TABLE },
    });
    const onDone = vi.fn();
    const user = userEvent.setup();
    render(
      <UndropDialog
        row={dropped()}
        environment="prod"
        onDone={onDone}
        onCancel={vi.fn()}
        nameTaken={null}
      />,
    );

    const button = screen.getByRole("button", { name: /Undrop/ });
    expect(button).toHaveAttribute("aria-disabled", "true");
    protection.release({ status: 200, body: { name: "prod", is_protected: true } });
    const reason = await screen.findByRole("textbox", {
      name: /Why you break the protection of prod/,
    });
    expect(screen.getByRole("button", { name: "Undrop" })).toBeDisabled();

    await user.type(reason, "Dropped by mistake");
    await user.click(screen.getByRole("button", { name: "Undrop" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledWith(TABLE);
    });
    expect(server.sent.at(-1)?.body).toEqual({
      new_name: null,
      break_glass_reason: "Dropped by mistake",
    });
  });

  it("takes the reason on one line: Enter sends it, and sends nothing while it is empty", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      [UNDROP]: { status: 200, body: TABLE },
    });
    const onDone = vi.fn();
    const user = userEvent.setup();
    render(
      <UndropDialog
        row={dropped()}
        environment="prod"
        onDone={onDone}
        onCancel={vi.fn()}
        nameTaken={null}
      />,
    );

    const field = await screen.findByRole("textbox", { name: /Why you break the protection/ });
    expect(field.tagName).toBe("INPUT");
    await user.type(field, "{Enter}");
    expect(onDone).not.toHaveBeenCalled();
    await user.type(field, "Wrong data in prod{Enter}");

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledWith(TABLE);
    });
    expect(server.sent.at(-1)?.body).toEqual({
      new_name: null,
      break_glass_reason: "Wrong data in prod",
    });
  });

  it("shows the server's sentence for a refusal and keeps the dialog open", async () => {
    fakeServer({
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: false } },
      [UNDROP]: {
        status: 422,
        body: {
          type: "ValidationError",
          detail: "The retention window has passed. Create it again.",
        },
      },
    });
    const user = userEvent.setup();
    render(
      <UndropDialog
        row={dropped()}
        environment="prod"
        onDone={vi.fn()}
        onCancel={vi.fn()}
        nameTaken={null}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Undrop" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The retention window has passed. Create it again.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("asks for another name when the server says the name is taken", async () => {
    const server = fakeServer({
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: false } },
      [UNDROP]: { status: 409, body: { type: "AlreadyExistsError", detail: TAKEN } },
    });
    const user = userEvent.setup();
    render(
      <UndropDialog
        row={dropped()}
        environment="prod"
        onDone={vi.fn()}
        onCancel={vi.fn()}
        nameTaken={null}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Undrop" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(TAKEN);
    expect(screen.getByRole("textbox", { name: "Restore as" })).toBeInTheDocument();
    expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(1);
  });
});
