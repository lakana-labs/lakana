import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { TableRef } from "../../api";
import { manageRoutes } from "../../test/manage";
import { fakeServer } from "../../test/server";
import { MaintainDialog } from "./MaintainDialog";

const PLACE: TableRef = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };
const MAINTENANCE =
  "POST /api/v1/environments/prod/namespaces/gold.sales/tables/fact_sales/maintenance";

/**
 * Renders the dialog.
 *
 * @param place - The table the dialog acts on.
 * @returns The close handler and a user-event session.
 */
function renderMaintain(place: TableRef = PLACE) {
  const onClose = vi.fn();
  const user = userEvent.setup();
  render(<MaintainDialog place={place} onClose={onClose} />);
  return { onClose, user };
}

/**
 * The maintenance requests the interface sent.
 *
 * @param sent - Every request.
 * @returns The maintenance requests.
 */
function maintenances<T extends { method: string; path: string }>(sent: readonly T[]): T[] {
  return sent.filter(
    (request) => request.method === "POST" && request.path.endsWith("/maintenance"),
  );
}

describe("MaintainDialog", () => {
  it.each(["a b", "$(x)"])(
    "quotes the environment %s in the command to copy",
    async (environment) => {
      fakeServer({
        ...manageRoutes(),
        [`POST /api/v1/environments/${environment}/namespaces/gold.sales/tables/fact_sales/maintenance`]:
          { status: 202, body: { query_id: "q-1" } },
      });
      const { user } = renderMaintain({ ...PLACE, environment });

      await user.click(screen.getByRole("button", { name: "Maintain now" }));

      expect(
        await screen.findByText(`lakana query status 'q-1' --env '${environment}'`),
      ).toBeInTheDocument();
    },
  );

  it("shows the defaults 7, 5, 72 and dry run off", () => {
    fakeServer(manageRoutes());
    renderMaintain();

    expect(screen.getByRole("dialog")).toHaveAccessibleName("Maintain gold.sales.fact_sales now");
    expect(screen.getByRole("textbox", { name: "Keep snapshots younger than (days)" })).toHaveValue(
      "7",
    );
    expect(screen.getByRole("textbox", { name: "Always keep the latest snapshots" })).toHaveValue(
      "5",
    );
    expect(
      screen.getByRole("textbox", { name: "Delete unreferenced files older than (hours)" }),
    ).toHaveValue("72");
    expect(
      screen.getByRole("checkbox", { name: "Dry run: report, change nothing" }),
    ).not.toBeChecked();
  });

  it("sends the options and shows Maintenance is queued as query {id} with the command to follow it", async () => {
    const server = fakeServer(manageRoutes());
    const { user } = renderMaintain();

    const days = screen.getByRole("textbox", { name: "Keep snapshots younger than (days)" });
    await user.clear(days);
    await user.type(days, "14");
    const last = screen.getByRole("textbox", { name: "Always keep the latest snapshots" });
    await user.clear(last);
    await user.type(last, "3");
    await user.click(screen.getByRole("checkbox", { name: "Dry run: report, change nothing" }));
    await user.click(screen.getByRole("button", { name: "Maintain now" }));

    const done = await screen.findByText("Maintenance is queued as query q-1.");
    expect(done).toHaveFocus();
    expect(screen.getByText("lakana query status 'q-1' --env 'prod'")).toBeInTheDocument();
    const [sent] = maintenances(server.sent);
    expect(sent?.body).toEqual({
      keep_days: 14,
      keep_last: 3,
      dry_run: true,
      older_than_hours: 72,
    });
  });

  it("sends the defaults untouched", async () => {
    const server = fakeServer(manageRoutes());
    const { user } = renderMaintain();

    await user.click(screen.getByRole("button", { name: "Maintain now" }));

    await screen.findByText("Maintenance is queued as query q-1.");
    expect(maintenances(server.sent)[0]?.body).toEqual({
      keep_days: 7,
      keep_last: 5,
      dry_run: false,
      older_than_hours: 72,
    });
  });

  it("closes with Close after it queued and with Cancel before", async () => {
    fakeServer(manageRoutes());
    const { onClose, user } = renderMaintain();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Maintain now" }));
    await screen.findByText("Maintenance is queued as query q-1.");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("shows the server's refusal word for word and stays open", async () => {
    const sentence =
      "You may not maintain gold.sales.fact_sales: it needs the MAINTAIN_TABLE right.";
    fakeServer({
      ...manageRoutes(),
      [MAINTENANCE]: { status: 403, body: { type: "PermissionDenied", detail: sentence } },
    });
    const { onClose, user } = renderMaintain();

    await user.click(screen.getByRole("button", { name: "Maintain now" }));

    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(screen.getByRole("button", { name: "Maintain now" })).toBeEnabled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("sends a value out of range as typed, so the server's sentence shows and nothing is clamped", async () => {
    const sentence = "older_than_hours must be at most 87600.";
    const server = fakeServer({
      ...manageRoutes(),
      [MAINTENANCE]: { status: 422, body: { type: "ValidationError", detail: sentence } },
    });
    const { user } = renderMaintain();

    const hours = screen.getByRole("textbox", {
      name: "Delete unreferenced files older than (hours)",
    });
    await user.clear(hours);
    await user.type(hours, "100000");
    await user.tab();
    expect(hours).toHaveValue("100000");
    await user.click(screen.getByRole("button", { name: "Maintain now" }));

    expect((await screen.findByRole("alert")).textContent).toBe(sentence);
    expect(maintenances(server.sent)[0]?.body).toMatchObject({ older_than_hours: 100000 });
  });
});
