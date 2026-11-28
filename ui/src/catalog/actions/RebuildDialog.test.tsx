import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type { TableRef } from "../../api";
import { manageRoutes, REBUILT } from "../../test/manage";
import { held } from "../../test/pending";
import { fakeServer } from "../../test/server";
import { fakeWebAuthn } from "../../test/webauthn";
import { RebuildDialog } from "./RebuildDialog";

const PLACE: TableRef = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const REBUILD = "POST /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales/rebuild";
const REASON = "History columns lost after a manual replace";

/**
 * Renders the dialog on the table's page, in a router whose location a test can read.
 *
 * @param place - The table the dialog acts on.
 * @returns The router, the close handler and a user-event session.
 */
function renderRebuild(place: TableRef = PLACE) {
  const onClose = vi.fn();
  const router = createMemoryRouter(
    [
      {
        path: "/catalog/prod/gold.sales/fact_sales",
        element: <RebuildDialog place={place} onClose={onClose} />,
      },
      { path: "/catalog", element: <p>The catalog</p> },
    ],
    { initialEntries: ["/catalog/prod/gold.sales/fact_sales"] },
  );
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);
  return { router, onClose, user };
}

/**
 * The rebuilds the interface sent.
 *
 * @param sent - Every request.
 * @returns The rebuild requests.
 */
function rebuilds<T extends { method: string; path: string }>(sent: readonly T[]): T[] {
  return sent.filter((request) => request.method === "POST" && request.path.includes("/rebuild"));
}

describe("RebuildDialog", () => {
  it("keeps Rebuild disabled until a reason is typed", async () => {
    const server = fakeServer(manageRoutes());
    const { user } = renderRebuild();

    expect(screen.getByRole("dialog")).toHaveAccessibleName(
      "Rebuild gold.sales.fact_sales from git?",
    );
    expect(screen.getByRole("dialog")).toHaveTextContent(
      "It drops the table, which you can restore like any drop, and queues a run of the asset that builds it.",
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Rebuild" })).toBeDisabled();
    });
    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      "   ",
    );
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeDisabled();
    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      "x",
    );
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeEnabled();
    expect(rebuilds(server.sent)).toEqual([]);
  });

  it("sends the reason and shows the run, the repository, the file and the command to follow it", async () => {
    const server = fakeServer(manageRoutes());
    const { user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));

    const done = await screen.findByText(
      `Dropped gold.sales.fact_sales and queued run ${REBUILT.run_id} of sales-project, which builds it again from assets/fact_sales.py.`,
    );
    expect(done).toHaveFocus();
    expect(
      screen.getByText(`lakana runs show '${REBUILT.run_id}' --env 'prod'`),
    ).toBeInTheDocument();
    const [sent] = rebuilds(server.sent);
    expect(sent?.body).toEqual({ reason: REASON });
    expect(new URL(sent?.path ?? "", "http://x").searchParams.has("break_glass_reason")).toBe(
      false,
    );
  });

  it.each(["a b", "$(x)"])(
    "quotes the environment %s in the command to copy",
    async (environment) => {
      fakeServer({
        ...manageRoutes(),
        [`POST /api/v1/environments/${environment}/namespaces/gold.sales/tables/fact_sales/rebuild`]:
          {
            status: 202,
            body: REBUILT,
          },
      });
      const { user } = renderRebuild({ ...PLACE, environment });

      await user.type(
        screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
        REASON,
      );
      await user.click(await screen.findByRole("button", { name: "Rebuild" }));

      expect(
        await screen.findByText(`lakana runs show '${REBUILT.run_id}' --env '${environment}'`),
      ).toBeInTheDocument();
    },
  );

  it("leaves the file out when the answer has no path", async () => {
    fakeServer({ ...manageRoutes(), [REBUILD]: { status: 202, body: { ...REBUILT, path: null } } });
    const { user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));

    expect(
      await screen.findByText(
        `Dropped gold.sales.fact_sales and queued run ${REBUILT.run_id} of sales-project.`,
      ),
    ).toBeInTheDocument();
  });

  it("marks a bidi character in the repository and the file", async () => {
    fakeServer({
      ...manageRoutes(),
      [REBUILD]: {
        status: 202,
        body: { ...REBUILT, repository: "sales\u202E", path: "a\u202E.py" },
      },
    });
    const { user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));

    expect(
      await screen.findByText(
        `Dropped gold.sales.fact_sales and queued run ${REBUILT.run_id} of salesU+202E, which builds it again from aU+202E.py.`,
      ),
    ).toBeInTheDocument();
  });

  it("runs the passkey touch on StepUpRequired and rebuilds again", async () => {
    const webauthn = fakeWebAuthn();
    let tries = 0;
    const server = fakeServer({
      ...manageRoutes(),
      [REBUILD]: () => {
        tries += 1;
        return tries === 1
          ? { status: 403, body: { type: "StepUpRequired", detail: "Confirm with your passkey." } }
          : { status: 202, body: REBUILT };
      },
      "POST /api/v1/sign-in/step-up/options": {
        status: 200,
        body: { ceremony_id: "c-1", public_key: {} },
      },
      "POST /api/v1/sign-in/step-up": { status: 204 },
    });
    const { user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));

    expect(await screen.findByText(/queued run/)).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(
      server.sent.filter((request) => request.method !== "GET").map((request) => request.path),
    ).toEqual([
      "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales/rebuild",
      "/api/v1/sign-in/step-up/options",
      "/api/v1/sign-in/step-up",
      "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales/rebuild",
    ]);
  });

  it("rebuilds again after the touch exactly as before, with the reason in the body and the query", async () => {
    fakeWebAuthn();
    let tries = 0;
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      [REBUILD]: () => {
        tries += 1;
        return tries === 1
          ? { status: 403, body: { type: "StepUpRequired", detail: "Confirm with your passkey." } }
          : { status: 202, body: REBUILT };
      },
      "POST /api/v1/sign-in/step-up/options": {
        status: 200,
        body: { ceremony_id: "c-1", public_key: {} },
      },
      "POST /api/v1/sign-in/step-up": { status: 204 },
    });
    const { user } = renderRebuild();

    await screen.findByText(/prod is protected/);
    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(screen.getByRole("button", { name: "Rebuild" }));

    expect(await screen.findByText(/queued run/)).toBeInTheDocument();
    const sent = rebuilds(server.sent);
    expect(sent).toHaveLength(2);
    const [first, retry] = sent;
    expect(retry?.path).toBe(first?.path);
    expect(first?.body).toEqual({ reason: REASON });
    expect(retry?.body).toEqual({ reason: REASON });
    expect(new URL(retry?.path ?? "", "http://x").searchParams.get("break_glass_reason")).toBe(
      REASON,
    );
  });

  it("in a protected environment sends the reason as break_glass_reason too and says so", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
    });
    const { user } = renderRebuild();

    expect(
      await screen.findByText(
        "prod is protected: only runs write there. Your reason is also sent as the reason you break its protection, which needs the right to manage the environment.",
      ),
    ).toBeInTheDocument();
    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(screen.getByRole("button", { name: "Rebuild" }));

    expect(await screen.findByText(/queued run/)).toBeInTheDocument();
    const [sent] = rebuilds(server.sent);
    expect(sent?.body).toEqual({ reason: REASON });
    expect(new URL(sent?.path ?? "", "http://x").searchParams.get("break_glass_reason")).toBe(
      REASON,
    );
  });

  it("shows the server's 422 for a short reason in a protected environment word for word", async () => {
    const sentence = "A break-glass reason needs 10 to 500 characters.";
    fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      [REBUILD]: { status: 422, body: { type: "ValidationError", detail: sentence } },
    });
    const { user } = renderRebuild();

    await screen.findByText(/prod is protected/);
    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      "short",
    );
    await user.click(screen.getByRole("button", { name: "Rebuild" }));

    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeInTheDocument();
  });

  it("takes the reason on one line: Enter sends it and never adds a line break", async () => {
    const server = fakeServer(manageRoutes());
    const { user } = renderRebuild();

    const field = screen.getByRole("textbox", {
      name: "Why it is rebuilt (recorded in the audit log)",
    });
    expect(field.tagName).toBe("INPUT");
    await user.type(field, `${REASON}{Enter}`);

    expect(await screen.findByText(/queued run/)).toBeInTheDocument();
    expect(rebuilds(server.sent)).toHaveLength(1);
    expect(rebuilds(server.sent)[0]?.body).toEqual({ reason: REASON });
  });

  it("does not send on Enter while the reason is empty", async () => {
    const server = fakeServer(manageRoutes());
    const { user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      "{Enter}",
    );

    expect(rebuilds(server.sent)).toEqual([]);
  });

  it("ignores Escape while the rebuild request is open, then shows its result", async () => {
    const pending = held();
    fakeServer({ ...manageRoutes(), [REBUILD]: pending.route });
    const { onClose, user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));
    await user.keyboard("{Escape}");

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    pending.release({ status: 202, body: REBUILT });
    expect(await screen.findByText(/queued run/)).toBeInTheDocument();
  });

  it("ignores the × while the rebuild request is open, as Cancel and Escape, then the × goes where Close does", async () => {
    const pending = held();
    fakeServer({ ...manageRoutes(), [REBUILD]: pending.route });
    const { router, onClose, user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));
    await user.click(screen.getByRole("button", { name: "Close dialog" }));

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    pending.release({ status: 202, body: REBUILT });
    expect(await screen.findByText(/queued run/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close dialog" }));
    await waitFor(() => {
      expect(router.state.location.pathname + router.state.location.search).toBe(
        "/catalog?env=prod&ns=gold.sales",
      );
    });
  });

  it("Close goes to the catalog place of the table", async () => {
    fakeServer(manageRoutes());
    const { router, onClose, user } = renderRebuild();

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));
    await user.click(await screen.findByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(router.state.location.pathname + router.state.location.search).toBe(
        "/catalog?env=prod&ns=gold.sales",
      );
    });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("goes to the catalog place on Escape once the table is rebuilt, and closes before", async () => {
    fakeServer(manageRoutes());
    const { router, onClose, user } = renderRebuild();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.type(
      screen.getByRole("textbox", { name: "Why it is rebuilt (recorded in the audit log)" }),
      REASON,
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild" }));
    await screen.findByText(/queued run/);
    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/catalog");
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("marks a hidden character of a protected environment's name", async () => {
    fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/pr\u202Eod": {
        status: 200,
        body: { name: "pr\u202Eod", is_protected: true },
      },
    });
    renderRebuild({ ...PLACE, environment: "pr\u202Eod" });

    expect(await screen.findByText(/^prU\+202Eod is protected/u)).toBeInTheDocument();
    expect(screen.getByRole("dialog").textContent).not.toMatch(/\u202E/u);
  });

  it("marks hidden characters in the server's refusal, whose sentence names stored things", async () => {
    fakeServer({
      ...manageRoutes(),
      [REBUILD]: {
        status: 409,
        body: {
          type: "ConflictError",
          detail: "Run 42 of analy\u202Etics is already active for gold.fact_\u2066sales.",
        },
      },
    });
    const { user } = renderRebuild();

    await user.type(await screen.findByRole("textbox", { name: /^Why it is rebuilt/u }), REASON);
    await user.click(screen.getByRole("button", { name: "Rebuild" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Run 42 of analyU+202Etics is already active for gold.fact_U+2066sales.",
    );
    expect(alert.textContent).not.toMatch(/[\u202E\u2066]/u);
  });
});
