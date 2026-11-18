import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, it } from "vitest";

import type { CheckResultOut } from "../api/jobs";
import { JOB_RUN_ID } from "../test/jobs";
import { fakeServer } from "../test/server";
import { ShowRowsButton } from "./ShowRowsButton";

const ROWS = `/api/v1/environments/prod/runs/${JOB_RUN_ID}/checks/rows`;

const CHECK: CheckResultOut = {
  name: "silver.orders/no_nulls",
  table: "silver.orders",
  outcome: "failed",
  failing_rows: 3,
};

/**
 * Shows where the router is, so a test can read it.
 *
 * @returns The location as text.
 */
function Where() {
  const location = useLocation();
  return <output aria-label="where">{location.pathname}</output>;
}

/**
 * Renders the button for `check`, beside a line that shows the address.
 *
 * @param check - The check.
 */
function renderButton(check: CheckResultOut = CHECK) {
  render(
    <MemoryRouter initialEntries={["/jobs/prod/runs/r1"]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <ShowRowsButton environment="prod" runId={JOB_RUN_ID} check={check} />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ShowRowsButton", () => {
  it("asks for the rows and opens the query's run on the Queries screen", async () => {
    const server = fakeServer({ [`POST ${ROWS}`]: { status: 201, body: { query_id: "q-9" } } });
    renderButton();

    await userEvent.click(
      screen.getByRole("button", { name: "Show rows of silver.orders/no_nulls" }),
    );

    await waitFor(() => {
      expect(screen.getByLabelText("where")).toHaveTextContent("/queries/prod/runs/q-9");
    });
    const sent = server.sent.find((request) => request.path === ROWS);
    expect(sent?.body).toEqual({ check: "silver.orders/no_nulls" });
  });

  it("a refusal without the right to run queries shows the servers sentence beside the button", async () => {
    fakeServer({
      [`POST ${ROWS}`]: { status: 403, body: { detail: "You may not run queries in prod." } },
    });
    renderButton();

    await userEvent.click(screen.getByRole("button", { name: /Show rows of/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("You may not run queries in prod.");
    expect(screen.getByLabelText("where")).toHaveTextContent("/jobs/prod/runs/r1");
    expect(screen.getByRole("button", { name: /Show rows of/ })).toBeEnabled();
  });

  it("marks a hidden character of the check's name", () => {
    renderButton({ ...CHECK, name: "silver.orders/no‮nulls" });

    expect(screen.getByRole("button")).toHaveTextContent(
      "Show rows of silver.orders/noU+202Enulls",
    );
  });
});
