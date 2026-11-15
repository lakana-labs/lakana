import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TableRef } from "../../api";
import { manageRoutes, retention } from "../../test/manage";
import { fakeServer } from "../../test/server";
import { RetentionDialog } from "./RetentionDialog";

const PLACE: TableRef = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const TABLE = "/api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales";
const GET = `GET ${TABLE}/retention`;
const PUT = `PUT ${TABLE}/retention`;

/**
 * Renders the dialog.
 *
 * @returns The close handler and a user-event session.
 */
function renderRetention() {
  const onClose = vi.fn();
  const user = userEvent.setup();
  render(<RetentionDialog place={PLACE} onClose={onClose} />);
  return { onClose, user };
}

/**
 * The retention writes the interface sent.
 *
 * @param sent - Every request.
 * @returns The PUT requests.
 */
function puts<T extends { method: string }>(sent: readonly T[]): T[] {
  return sent.filter((request) => request.method === "PUT");
}

describe("RetentionDialog", () => {
  it("reads and shows the current retention and its expiry", async () => {
    fakeServer(manageRoutes());
    renderRetention();

    expect(
      await screen.findByText("30 days after its last activity, set for the table by ana"),
    ).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Retention of gold.sales.fact_sales");
    expect(screen.getByRole("dialog")).toHaveTextContent(/Expires .*2026/);
    expect(screen.getByRole("radio", { name: "After days without activity" })).toBeChecked();
    expect(screen.getByRole("textbox", { name: "Days" })).toHaveValue("30");
  });

  it("says when there is no retention and offers the three rules", async () => {
    fakeServer({
      ...manageRoutes(),
      [GET]: {
        status: 200,
        body: retention({ source: null, days: null, set_by: null, expires_at: null }),
      },
    });
    renderRetention();

    expect(await screen.findByText("No retention: it never expires.")).toBeInTheDocument();
    expect(
      screen.getAllByRole("radio").map((radio) => radio.closest("label")?.textContent),
    ).toEqual(["After days without activity", "On a day", "Never"]);
  });

  it.each([
    [
      "days",
      async (user: ReturnType<typeof userEvent.setup>) => {
        const field = screen.getByRole("textbox", { name: "Days" });
        await user.clear(field);
        await user.type(field, "45");
      },
      { days: 45 },
    ],
    [
      "on",
      async (user: ReturnType<typeof userEvent.setup>) => {
        await user.click(screen.getByRole("radio", { name: "On a day" }));
        await user.type(screen.getByLabelText("Day"), "2026-12-01");
      },
      { on: "2026-12-01" },
    ],
    [
      "never",
      async (user: ReturnType<typeof userEvent.setup>) => {
        await user.click(screen.getByRole("radio", { name: "Never" }));
      },
      { never: true },
    ],
  ])("sends exactly one rule: %s", async (_name, choose, body) => {
    const server = fakeServer(manageRoutes());
    const { user } = renderRetention();
    await screen.findByText(/30 days after/);

    await choose(user);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(puts(server.sent).map((request) => request.body)).toEqual([body]);
  });

  it("shows the new rule after saving, and Close closes", async () => {
    fakeServer({
      ...manageRoutes(),
      [PUT]: {
        status: 200,
        body: retention({ days: null, never: true, expires_at: null }),
      },
    });
    const { onClose, user } = renderRetention();
    await screen.findByText(/30 days after/);

    await user.click(screen.getByRole("radio", { name: "Never" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    const done = await screen.findByText("never, set for the table by ana");
    expect(done).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("says a deployed project keeps the table", async () => {
    fakeServer({
      ...manageRoutes(),
      [GET]: { status: 200, body: retention({ kept: "project_asset", expires_at: null }) },
    });
    renderRetention();

    expect(
      await screen.findByText(
        "A deployed project declares this table, so it does not expire while it is deployed. After that: 30 days after its last activity, set for the table by ana.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("says a protected environment keeps the table", async () => {
    fakeServer({
      ...manageRoutes(),
      [GET]: { status: 200, body: retention({ kept: "protected", expires_at: null }) },
    });
    renderRetention();

    expect(
      await screen.findByText("Tables in a protected environment never expire."),
    ).toBeInTheDocument();
  });

  it("shows the server's 422 for days in a protected environment word for word", async () => {
    const sentence = "prod is protected: a table there cannot be given an expiry. Use never.";
    fakeServer({
      ...manageRoutes(),
      [PUT]: { status: 422, body: { type: "ValidationError", detail: sentence } },
    });
    const { onClose, user } = renderRetention();
    await screen.findByText(/30 days after/);

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("shows why reading failed and offers Cancel", async () => {
    const sentence = "You may not read this table's retention.";
    fakeServer({
      ...manageRoutes(),
      [GET]: { status: 403, body: { type: "PermissionDenied", detail: sentence } },
    });
    const { onClose, user } = renderRetention();

    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("starts on the rule the table has: a day", async () => {
    fakeServer({
      ...manageRoutes(),
      [GET]: { status: 200, body: retention({ days: null, on: "2026-12-01" }) },
    });
    renderRetention();

    expect(await screen.findByRole("radio", { name: "On a day" })).toBeChecked();
    expect(screen.getByLabelText("Day")).toHaveValue("2026-12-01");
  });

  it("starts on the rule the table has: never", async () => {
    fakeServer({
      ...manageRoutes(),
      [GET]: { status: 200, body: retention({ days: null, never: true, expires_at: null }) },
    });
    renderRetention();

    expect(await screen.findByRole("radio", { name: "Never" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("keeps Save disabled until the chosen rule has its days or its day", async () => {
    fakeServer({
      ...manageRoutes(),
      [GET]: {
        status: 200,
        body: retention({ source: null, days: null, set_by: null, expires_at: null }),
      },
    });
    const { user } = renderRetention();

    await screen.findByText("No retention: it never expires.");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "On a day" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await user.type(screen.getByLabelText("Day"), "2026-12-01");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("shows the saved rule after saving on a table a deployed project builds", async () => {
    fakeServer({
      ...manageRoutes(),
      [PUT]: {
        status: 200,
        body: retention({ kept: "project_asset", days: 45, expires_at: null }),
      },
    });
    const { user } = renderRetention();
    await screen.findByText(/30 days after/);

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText(/does not expire while it is deployed\. After that: 45 days after/),
    ).toBeInTheDocument();
  });

  it("shows the server's 403 for a shortening without the right to drop word for word", async () => {
    const sentence = "You may make a table expire sooner only with the right to drop it.";
    fakeServer({
      ...manageRoutes(),
      [PUT]: { status: 403, body: { type: "PermissionDenied", detail: sentence } },
    });
    const { onClose, user } = renderRetention();
    await screen.findByText(/30 days after/);

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("sends a number out of range as typed, so the server's sentence shows and nothing is clamped", async () => {
    const sentence = "Days must be between 1 and 3650.";
    const server = fakeServer({
      ...manageRoutes(),
      [PUT]: { status: 422, body: { type: "ValidationError", detail: sentence } },
    });
    const { user } = renderRetention();
    await screen.findByText(/30 days after/);

    const field = screen.getByRole("textbox", { name: "Days" });
    await user.clear(field);
    await user.type(field, "5000");
    await user.tab();
    expect(field).toHaveValue("5000");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(puts(server.sent).map((request) => request.body)).toEqual([{ days: 5000 }]);
  });

  it("offers only Never in a protected environment and says why", async () => {
    const server = fakeServer({
      ...manageRoutes(),
      "GET /api/v1/environments/prod": { status: 200, body: { name: "prod", is_protected: true } },
      [GET]: {
        status: 200,
        body: retention({ source: null, days: null, set_by: null, expires_at: null }),
      },
    });
    const { user } = renderRetention();

    expect(
      await screen.findByText(
        "prod is protected, so its tables never expire and no days or day can be set there. Never is still accepted, to remove a rule set before the protection.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "After days without activity" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "On a day" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Never" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByRole("button", { name: "Close" });
    expect(puts(server.sent).map((request) => request.body)).toEqual([{ never: true }]);
  });

  it("offers every rule when the environment is not protected", async () => {
    fakeServer(manageRoutes());
    renderRetention();
    await screen.findByText(/30 days after/);

    expect(screen.getByRole("radio", { name: "After days without activity" })).toBeEnabled();
    expect(screen.getByRole("radio", { name: "On a day" })).toBeEnabled();
  });
});
