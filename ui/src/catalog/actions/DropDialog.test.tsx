import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TableRef } from "../../api";
import { manageRoutes } from "../../test/manage";
import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { fakeWebAuthn } from "../../test/webauthn";
import { DropDialog } from "./DropDialog";

const REF: TableRef = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const DROP = "DELETE /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales";
const STEP_UP_REQUIRED = {
  status: 403,
  body: { type: "StepUpRequired", detail: "Confirm with your passkey." },
};
const GIT_OWNED =
  "`silver.clean` is built by a deployed project. Defined in analytics/assets/silver/clean.sql. Remove it there and deploy; then drop it from Orphans.";

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
 * Renders the dialog for `gold.sales.fact_sales`, or another target.
 *
 * @param kind - The target's kind.
 * @param ref - The target.
 * @returns The handlers and a user-event session.
 */
function renderDrop(kind: "TABLE" | "ARTIFACT" | null = "TABLE", ref: TableRef = REF) {
  const onDone = vi.fn();
  const onCancel = vi.fn();
  const user = userEvent.setup();
  render(<DropDialog target={{ ref, kind }} onDone={onDone} onCancel={onCancel} />);
  return { onDone, onCancel, user };
}

describe("DropDialog", () => {
  it("asks first, then sends one DELETE and says it is done", async () => {
    const server = fakeServer(manageRoutes());
    const { onDone, user } = renderDrop();

    expect(screen.getByRole("dialog")).toHaveAccessibleName("Drop gold.sales.fact_sales?");
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "It leaves the catalog with its grants, row filters and column masks. You can restore it from Dropped during the undo period (7 days unless your operator changed it).",
    );
    expect(writes(server.sent)).toEqual([]);

    await user.click(await screen.findByRole("button", { name: "Drop the table" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales",
    ]);
  });

  it("sends purge=true when Delete the files now is checked and says it cannot be restored", async () => {
    const server = fakeServer(manageRoutes());
    const { onDone, user } = renderDrop();

    await user.click(
      screen.getByRole("checkbox", {
        name: "Delete the files now: the table cannot be restored",
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Drop the table" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales?purge=true",
    ]);
  });

  it("runs the passkey touch on StepUpRequired and sends the drop again", async () => {
    const webauthn = fakeWebAuthn();
    let tries = 0;
    const server = fakeServer({
      ...manageRoutes(),
      [DROP]: () => {
        tries += 1;
        return tries === 1 ? STEP_UP_REQUIRED : { status: 204 };
      },
      "POST /api/v1/sign-in/step-up/options": {
        status: 200,
        body: { ceremony_id: "c-1", public_key: {} },
      },
      "POST /api/v1/sign-in/step-up": { status: 204 },
    });
    const { onDone, user } = renderDrop();

    await user.click(await screen.findByRole("button", { name: "Drop the table" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales",
    ]);
  });

  it("marks hidden characters of a protected environment's name in the reason's label and help", async () => {
    fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/pr\u202Eod": {
        status: 200,
        body: { name: "pr\u202Eod", is_protected: true },
      },
    });
    renderDrop("TABLE", { ...REF, environment: "pr\u202Eod" });

    const reason = await screen.findByRole("textbox", {
      name: /Why you break the protection of prU\+202Eod/,
    });
    expect(reason).toBeInTheDocument();
    expect(screen.getByRole("dialog").textContent).not.toContain("\u202E");
  });

  it("sends the drop again after the touch exactly as before, with purge and the reason", async () => {
    fakeWebAuthn();
    let tries = 0;
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      [DROP]: () => {
        tries += 1;
        return tries === 1 ? STEP_UP_REQUIRED : { status: 204 };
      },
      "POST /api/v1/sign-in/step-up/options": {
        status: 200,
        body: { ceremony_id: "c-1", public_key: {} },
      },
      "POST /api/v1/sign-in/step-up": { status: 204 },
    });
    const { onDone, user } = renderDrop();

    await user.type(
      await screen.findByRole("textbox", { name: /Why you break the protection of prod/ }),
      "Wrong data in prod",
    );
    await user.click(
      screen.getByRole("checkbox", { name: "Delete the files now: the table cannot be restored" }),
    );
    await user.click(screen.getByRole("button", { name: "Drop the table" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    const drops = server.sent.filter((request) => request.method === "DELETE");
    expect(drops).toHaveLength(2);
    const [first, retry] = drops;
    expect(retry?.path).toBe(first?.path);
    expect(retry?.body).toEqual(first?.body);
    const query = new URL(first?.path ?? "", "http://x").searchParams;
    expect(query.get("purge")).toBe("true");
    expect(query.get("break_glass_reason")).toBe("Wrong data in prod");
  });

  it("takes the break-glass reason on one line: Enter sends it and never adds a line break", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
    });
    const { onDone, user } = renderDrop();

    const field = await screen.findByRole("textbox", { name: /Why/ });
    expect(field.tagName).toBe("INPUT");
    await user.type(field, "Wrong data in prod{Enter}");

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    const query = new URL(
      server.sent.find((request) => request.method === "DELETE")?.path ?? "",
      "http://x",
    ).searchParams;
    expect(query.get("break_glass_reason")).toBe("Wrong data in prod");
  });

  it("does not send on Enter while the break-glass reason is empty", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
    });
    const { user } = renderDrop();

    await user.type(await screen.findByRole("textbox", { name: /Why/ }), "{Enter}");

    expect(writes(server.sent)).toEqual([]);
  });

  it("keeps the dialog open with the reason when the passkey touch fails", async () => {
    fakeWebAuthn();
    const server = fakeServer({
      ...manageRoutes(),
      [DROP]: STEP_UP_REQUIRED,
      "POST /api/v1/sign-in/step-up/options": {
        status: 409,
        body: {
          type: "ConflictError",
          detail: "You have no passkey. Add one under Account, then try again.",
        },
      },
    });
    const { onDone, onCancel, user } = renderDrop();

    await user.click(await screen.findByRole("button", { name: "Drop the table" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You have no passkey. Add one under Account, then try again.",
    );
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales",
      "POST /api/v1/sign-in/step-up/options",
    ]);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("shows the server's git-owned refusal word for word and stays", async () => {
    fakeServer({
      ...manageRoutes(),
      [DROP]: { status: 409, body: { type: "ConflictError", detail: GIT_OWNED } },
    });
    const { onDone, user } = renderDrop();

    await user.click(await screen.findByRole("button", { name: "Drop the table" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(GIT_OWNED, {
      normalizeWhitespace: false,
    });
    expect(screen.getByRole("alert").textContent).toBe(GIT_OWNED);
    expect(onDone).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("drops an artifact through the artifact route without a purge box", async () => {
    const server = fakeServer(manageRoutes());
    const { onDone, user } = renderDrop("ARTIFACT", {
      environment: "prod",
      namespace: "ml",
      name: "model",
    });

    expect(screen.getByRole("dialog")).toHaveTextContent(
      "This cannot be undone: every version's files are deleted.",
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Drop the artifact" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    expect(writes(server.sent)).toEqual([
      "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model",
    ]);
  });

  it("waits for the protection, then asks for the break-glass reason and sends it", async () => {
    const protection = held();
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": protection.route,
    });
    const { onDone, user } = renderDrop();

    expect(screen.getByRole("button", { name: /Drop the table/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    protection.release({ status: 200, body: { name: "prod", is_protected: true } });
    const reason = await screen.findByRole("textbox", {
      name: /Why you break the protection of prod/,
    });
    expect(screen.getByRole("button", { name: "Drop the table" })).toBeDisabled();
    await user.type(reason, "Wrong data in prod");
    await user.click(screen.getByRole("button", { name: "Drop the table" }));

    await waitFor(() => {
      expect(onDone).toHaveBeenCalledTimes(1);
    });
    const [sent] = server.sent.filter((request) => request.method === "DELETE");
    expect(new URL(sent?.path ?? "", "http://x").searchParams.get("break_glass_reason")).toBe(
      "Wrong data in prod",
    );
  });

  it("names an orphan of unknown kind as a table", async () => {
    fakeServer(manageRoutes());
    renderDrop(null);

    expect(await screen.findByRole("button", { name: "Drop the table" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox")).toBeInTheDocument();
  });

  it("ignores Escape and Cancel while the drop is pending", async () => {
    const slow = held();
    fakeServer({ ...manageRoutes(), [DROP]: slow.route });
    const { onDone, onCancel, user } = renderDrop();

    await user.click(await screen.findByRole("button", { name: "Drop the table" }));
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
