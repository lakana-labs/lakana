import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { manageRoutes } from "../../test/manage";
import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { fakeWebAuthn } from "../../test/webauthn";
import { DeleteNamespaceDialog } from "./DeleteNamespaceDialog";

const DELETE = "DELETE /api/v1/environments/prod/namespaces/silver";
const STEP_UP_REQUIRED = {
  status: 403,
  body: { type: "StepUpRequired", detail: "Confirm with your passkey." },
};
const STEP_UP_ROUTES = {
  "POST /api/v1/sign-in/step-up/options": {
    status: 200,
    body: { ceremony_id: "c-1", public_key: {} },
  },
  "POST /api/v1/sign-in/step-up": { status: 204 },
};

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

/**
 * Renders the dialog for `prod.silver`.
 *
 * @returns The handlers and a user-event session.
 */
function renderDelete() {
  const onDone = vi.fn();
  const onCancel = vi.fn();
  const user = userEvent.setup();
  render(
    <DeleteNamespaceDialog
      environment="prod"
      namespace="silver"
      onDone={onDone}
      onCancel={onCancel}
    />,
  );
  return { onDone, onCancel, user };
}

describe("DeleteNamespaceDialog", () => {
  it("asks first, then sends one DELETE", async () => {
    const server = fakeServer(manageRoutes());
    const { onDone, user } = renderDelete();

    expect(screen.getByRole("dialog")).toHaveAccessibleName("Delete namespace silver?");
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "Only an empty namespace can be deleted. The grants given on it are revoked.",
    );
    expect(writes(server.sent)).toEqual([]);

    await user.click(await screen.findByRole("button", { name: "Delete the namespace" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(writes(server.sent)).toEqual(["DELETE /api/v1/environments/prod/namespaces/silver"]);
  });

  it("runs the passkey touch on StepUpRequired and deletes again", async () => {
    const webauthn = fakeWebAuthn();
    let tries = 0;
    const server = fakeServer({
      ...manageRoutes(),
      ...STEP_UP_ROUTES,
      [DELETE]: () => {
        tries += 1;
        return tries === 1 ? STEP_UP_REQUIRED : { status: 204 };
      },
    });
    const { onDone, user } = renderDelete();

    await user.click(await screen.findByRole("button", { name: "Delete the namespace" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/silver",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/environments/prod/namespaces/silver",
    ]);
  });

  it("keeps the dialog open with the reason when the passkey touch fails", async () => {
    fakeWebAuthn();
    fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      [DELETE]: STEP_UP_REQUIRED,
      "POST /api/v1/sign-in/step-up/options": {
        status: 409,
        body: {
          type: "ConflictError",
          detail: "You have no passkey. Add one under Account, then try again.",
        },
      },
    });
    const { onDone, onCancel, user } = renderDelete();

    await user.type(
      await screen.findByRole("textbox", { name: /Why you break the protection of prod/ }),
      "Clean up after the move",
    );
    await user.click(screen.getByRole("button", { name: "Delete the namespace" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You have no passkey. Add one under Account, then try again.",
    );
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Why you break/ })).toHaveValue(
      "Clean up after the move",
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it.each(["silver still holds 2 tables.", "silver is not empty; drop what it holds first."])(
    "shows the server's not-empty refusal word for word and stays: %s",
    async (detail) => {
      fakeServer({
        ...manageRoutes(),
        [DELETE]: { status: 409, body: { type: "ConflictError", detail } },
      });
      const { onDone, onCancel, user } = renderDelete();

      await user.click(await screen.findByRole("button", { name: "Delete the namespace" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(detail);
      expect(screen.getByRole("alert").textContent).toBe(detail);
      expect(onDone).not.toHaveBeenCalled();
      expect(screen.getByRole("dialog")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Cancel" }));
      expect(onCancel).toHaveBeenCalledTimes(1);
    },
  );

  it("in a protected environment sends the reason as break_glass_reason in the query", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
    });
    const { onDone, user } = renderDelete();

    const reason = await screen.findByRole("textbox", {
      name: /Why you break the protection of prod/,
    });
    expect(screen.getByRole("button", { name: "Delete the namespace" })).toBeDisabled();
    await user.type(reason, "Clean up after the move{Enter}");

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/silver?break_glass_reason=Clean+up+after+the+move",
    ]);
  });

  it("does not send on Enter while the reason is empty, nor twice while it is pending", async () => {
    const slow = held();
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      [DELETE]: slow.route,
    });
    const { onDone, user } = renderDelete();

    const reason = await screen.findByRole("textbox", { name: /Why you break/ });
    await user.type(reason, "{Enter}");
    expect(writes(server.sent)).toEqual([]);

    await user.type(reason, "Clean up after the move{Enter}{Enter}");
    slow.release({ status: 204 });
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(writes(server.sent)).toHaveLength(1);
  });

  it("ignores Escape while the request is pending", async () => {
    const slow = held();
    fakeServer({ ...manageRoutes(), [DELETE]: slow.route });
    const { onDone, onCancel, user } = renderDelete();

    await user.click(await screen.findByRole("button", { name: "Delete the namespace" }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    slow.release({ status: 204 });
    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
  });
});
