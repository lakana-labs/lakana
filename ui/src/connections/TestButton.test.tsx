import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type JSX, useState } from "react";
import { describe, expect, it } from "vitest";

import type { ConnectionOut } from "../api";
import { aConnection } from "../test/connections";
import { expectPending, held } from "../test/pending";
import { fakeServer } from "../test/server";
import { TestButton } from "./TestButton";
import { TestStatus } from "./TestStatus";
import { STUCK_AFTER_MS } from "./useOpenTests";

const NOW = Date.now();
const TESTS = "POST /api/v1/environments/prod/connections/crm/tests";
const CRM = "GET /api/v1/environments/prod/connections/crm";
const QUEUED = {
  id: "t1",
  state: "queued",
  outcome: null,
  message: null,
  // Just now: a test open for ten minutes enables Test again.
  created_at: new Date().toISOString(),
  finished_at: null,
};

/**
 * A row as the list draws it: the status and Test, the row replaced by what Test starts.
 *
 * @param props - The connection to start from.
 * @param props.start - The connection.
 * @returns The row.
 */
function Row({ start }: { readonly start: ConnectionOut }): JSX.Element {
  const [row, setRow] = useState(start);
  return (
    <div>
      <TestStatus connection={row} now={NOW} />
      <TestButton connection={row} at={NOW} onStarted={setRow} />
    </div>
  );
}

describe("TestButton", () => {
  it("posts a test once and the row says queued", async () => {
    const answer = held();
    const server = fakeServer({ [TESTS]: answer.route });
    const user = userEvent.setup();
    render(<Row start={aConnection()} />);

    await user.click(screen.getByRole("button", { name: "Test crm" }));
    expectPending(screen.getByRole("button", { name: /^Test crm/ }));
    answer.release({ status: 202, body: QUEUED });

    expect(await screen.findByText("queued")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Testing… crm" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Testing… crm" })).toHaveTextContent("Testing…");
    expect(server.sent).toEqual([
      expect.objectContaining({ method: "POST", path: TESTS.slice(5) }),
    ]);
    // The disabled button lost the focus; it stays beside it, never on the page.
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Testing… crm" }).closest(".test-button"),
    );
  });

  it("a 409 reads the connection again and follows its open test without a message", async () => {
    const server = fakeServer({
      [TESTS]: { status: 409, body: { detail: "A test is open.", type: "Conflict" } },
      [CRM]: {
        status: 200,
        body: aConnection({ last_test: { ...QUEUED, state: "running" } as const }),
      },
    });
    const user = userEvent.setup();
    render(<Row start={aConnection()} />);

    await user.click(screen.getByRole("button", { name: "Test crm" }));

    expect(await screen.findByText("testing…")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Testing… crm" })).toBeDisabled();
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([TESTS, CRM]);
  });

  it("says why when the connection cannot be read again after a 409", async () => {
    fakeServer({
      [TESTS]: { status: 409, body: { detail: "A test is open.", type: "Conflict" } },
      [CRM]: { status: 404, body: { detail: "No connection crm in prod.", type: "NotFound" } },
    });
    const user = userEvent.setup();
    render(<Row start={aConnection()} />);

    await user.click(screen.getByRole("button", { name: "Test crm" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("No connection crm in prod.");
    expect(screen.getByRole("button", { name: "Test crm" })).toBeEnabled();
  });

  it("shows another refusal beside the button, and clears it on the next press", async () => {
    let refuse = true;
    fakeServer({
      [TESTS]: () =>
        refuse
          ? {
              status: 403,
              body: { detail: "You may not test connections in prod.", type: "Forbidden" },
            }
          : { status: 202, body: QUEUED },
    });
    const user = userEvent.setup();
    render(<Row start={aConnection()} />);

    await user.click(screen.getByRole("button", { name: "Test crm" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You may not test connections in prod.",
    );

    refuse = false;
    await user.click(screen.getByRole("button", { name: "Test crm" }));
    await waitFor(() => {
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  });

  it("is disabled with words while the row's test is open", () => {
    const server = fakeServer();
    render(
      <TestButton
        at={Date.now()}
        connection={aConnection({
          last_test: { ...QUEUED, state: "queued" } as const,
        })}
        onStarted={() => undefined}
      />,
    );

    const button = screen.getByRole("button", { name: "Testing… crm" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Testing…");
    expect(server.sent).toHaveLength(0);
  });

  it("is enabled again for a test open for ten minutes, so Test again can close it", () => {
    render(
      <TestButton
        at={Date.now()}
        connection={aConnection({
          last_test: {
            ...QUEUED,
            state: "running",
            created_at: new Date(Date.now() - STUCK_AFTER_MS).toISOString(),
          } as const,
        })}
        onStarted={() => undefined}
      />,
    );

    expect(screen.getByRole("button", { name: "Test crm" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Test crm" })).toHaveTextContent(/^Test$/);
  });

  it("takes the present from the screen: the same test is open or stuck by the clock it is given", () => {
    const connection = aConnection({
      last_test: { ...QUEUED, state: "running" } as const,
    });
    const created = Date.parse(QUEUED.created_at);
    const { rerender } = render(
      <TestButton connection={connection} at={created} onStarted={() => undefined} />,
    );
    expect(screen.getByRole("button", { name: "Testing… crm" })).toBeDisabled();

    rerender(
      <TestButton
        connection={connection}
        at={created + STUCK_AFTER_MS}
        onStarted={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Test crm" })).toBeEnabled();
  });
});
