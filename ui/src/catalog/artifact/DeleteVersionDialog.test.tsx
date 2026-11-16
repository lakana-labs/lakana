import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TableRef } from "../../api";
import { artifactRoutes, DELETED, version } from "../../test/artifacts";
import { manageRoutes } from "../../test/manage";
import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { fakeWebAuthn } from "../../test/webauthn";
import { DeleteVersionDialog } from "./DeleteVersionDialog";

const PLACE: TableRef = { environment: "prod", namespace: "ml", name: "model" };
const DELETE = "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3";
const PROTECTED = {
  "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
};
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
const DONE =
  "Deleted version 3 of ml.model. Freed 1 file (6 B). Kept 1 file (7 B) that version 2 also uses.";

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
 * Renders the dialog for version 3 of `prod/ml/model` (13 B, 2 files).
 *
 * @param onGone - Called when the server says the version is gone.
 * @returns The handlers and a user-event session.
 */
function renderDelete(onGone?: () => void) {
  const onDone = vi.fn();
  const onCancel = vi.fn();
  const user = userEvent.setup();
  render(
    <DeleteVersionDialog
      place={PLACE}
      version={version()}
      onDone={onDone}
      onCancel={onCancel}
      {...(onGone === undefined ? {} : { onGone })}
    />,
  );
  return { onDone, onCancel, user };
}

describe("DeleteVersionDialog", () => {
  it("asks first with the size and the file count, then sends one DELETE", async () => {
    const server = fakeServer({ ...artifactRoutes(), ...manageRoutes() });
    const { onDone, user } = renderDelete();

    expect(screen.getByRole("dialog")).toHaveAccessibleName("Delete version 3 of ml.model?");
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "13 B, 2 files. Its bytes are removed from Lakana's storage. Lakana cannot bring them back. The version stays in the list, marked deleted, and its number is never used again.",
    );
    expect(writes(server.sent)).toEqual([]);

    await user.click(await screen.findByRole("button", { name: "Delete the version" }));

    expect(await screen.findByText(DONE)).toBeVisible();
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3",
    ]);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("runs the passkey touch on StepUpRequired and deletes again", async () => {
    const webauthn = fakeWebAuthn();
    let tries = 0;
    const server = fakeServer({
      ...artifactRoutes(),
      ...manageRoutes(),
      ...STEP_UP_ROUTES,
      [DELETE]: () => {
        tries += 1;
        return tries === 1 ? STEP_UP_REQUIRED : { status: 200, body: DELETED };
      },
    });
    const { user } = renderDelete();

    await user.click(await screen.findByRole("button", { name: "Delete the version" }));

    expect(await screen.findByText(DONE)).toBeVisible();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3",
    ]);
  });

  it("keeps the dialog open with the reason when the passkey touch fails", async () => {
    fakeWebAuthn();
    fakeServer({
      ...artifactRoutes(),
      ...manageRoutes(),
      ...PROTECTED,
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
      "Model 3 was trained on the wrong customers",
    );
    await user.click(screen.getByRole("button", { name: "Delete the version" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You have no passkey. Add one under Account, then try again.",
    );
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Why you break/ })).toHaveValue(
      "Model 3 was trained on the wrong customers",
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("shows the server's 409 for a version already deleted word for word", async () => {
    const detail = "Version 3 of ml.model is already deleted.";
    fakeServer({
      ...artifactRoutes(),
      ...manageRoutes(),
      [DELETE]: { status: 409, body: { type: "ConflictError", detail } },
    });
    const onGone = vi.fn();
    const { onDone, onCancel, user } = renderDelete(onGone);

    await user.click(await screen.findByRole("button", { name: "Delete the version" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("alert").textContent).toBe(detail);
    expect(onGone).toHaveBeenCalledTimes(1);
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("does not call the versions gone for a refusal of another kind", async () => {
    fakeServer({
      ...artifactRoutes(),
      ...manageRoutes(),
      [DELETE]: { status: 500, body: { detail: "The catalog database broke." } },
    });
    const onGone = vi.fn();
    const { user } = renderDelete(onGone);

    await user.click(await screen.findByRole("button", { name: "Delete the version" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The catalog database broke.");
    expect(onGone).not.toHaveBeenCalled();
  });

  it("requires a reason in a protected environment and sends it as break_glass_reason in the query", async () => {
    const server = fakeServer({ ...artifactRoutes(), ...manageRoutes(), ...PROTECTED });
    const { user } = renderDelete();

    const reason = await screen.findByRole("textbox", {
      name: /Why you break the protection of prod/,
    });
    expect(screen.getByRole("button", { name: "Delete the version" })).toBeDisabled();
    await user.type(reason, "{Enter}");
    expect(writes(server.sent)).toEqual([]);

    await user.type(reason, "  Model 3 was trained on the wrong customers {Enter}");

    expect(await screen.findByText(DONE)).toBeVisible();
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3?break_glass_reason=Model+3+was+trained+on+the+wrong+customers",
    ]);
  });

  it("ignores Escape and Cancel while the request is pending", async () => {
    const slow = held();
    const server = fakeServer({ ...artifactRoutes(), ...manageRoutes(), [DELETE]: slow.route });
    const { onCancel, user } = renderDelete();

    await user.click(await screen.findByRole("button", { name: "Delete the version" }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    slow.release({ status: 200, body: DELETED });
    expect(await screen.findByText(DONE)).toBeVisible();
    expect(writes(server.sent)).toHaveLength(1);
  });

  it("shows what was freed and kept with the focus, then Close calls onDone", async () => {
    fakeServer({ ...artifactRoutes(), ...manageRoutes() });
    const { onDone, onCancel, user } = renderDelete();

    await user.click(await screen.findByRole("button", { name: "Delete the version" }));

    const done = await screen.findByText(DONE);
    await waitFor(() => {
      expect(done).toHaveFocus();
    });
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Delete version 3 of ml.model?");
    expect(screen.queryByRole("button", { name: "Delete the version" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Close" }));

    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("closes the done body with Escape as with Close", async () => {
    fakeServer({ ...artifactRoutes(), ...manageRoutes() });
    const { onDone, user } = renderDelete();

    await user.click(await screen.findByRole("button", { name: "Delete the version" }));
    await screen.findByText(DONE);
    await user.keyboard("{Escape}");

    expect(onDone).toHaveBeenCalledTimes(1);
  });
});
