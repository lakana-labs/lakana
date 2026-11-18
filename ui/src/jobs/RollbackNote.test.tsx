import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DeployOut } from "../api/jobs";
import { aDeploy } from "../test/jobs";
import { type FakeAnswer, fakeServer } from "../test/server";
import { RollbackNote } from "./RollbackNote";

const ROLLBACK = "d-rollback";
const READ = `GET /api/v1/environments/prod/deploys/${ROLLBACK}`;

/**
 * Lets the reads and their renders finish.
 */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 5; turn += 1) {
    await act(async () => {
      await new Promise((resolve) => {
        setImmediate(resolve);
      });
    });
  }
}

/**
 * Renders the note in a status region over a server that answers the rollback in the states
 * given, one per read, the last one from then on.
 *
 * @param states - The rollback as each read answers it.
 * @returns The server and the end handler.
 */
function renderNote(states: readonly (Partial<DeployOut> | FakeAnswer)[]) {
  let reads = 0;
  const server = fakeServer({
    [READ]: () => {
      const state = states[Math.min(reads, states.length - 1)] ?? {};
      reads += 1;
      if (typeof state.status === "number") {
        return state;
      }
      return {
        status: 200,
        body: aDeploy({ id: ROLLBACK, kind: "rollback", ...state }),
      };
    },
  });
  const onEnded = vi.fn();
  render(
    <MemoryRouter>
      <div role="status">
        <RollbackNote
          environment="prod"
          repository="analytics"
          commit="4f9d2a7"
          deployId={ROLLBACK}
          onEnded={onEnded}
        />
      </div>
    </MemoryRouter>,
  );
  return { server, onEnded };
}

/**
 * Moves the clock on by a number of milliseconds and lets the reads finish.
 *
 * @param ms - How far.
 */
async function wait(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("RollbackNote", () => {
  it("says it is rolling back and reads the deploy every two seconds", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { server } = renderNote([{ status: "queued" }, { status: "running" }]);
    await settle();

    expect(screen.getByRole("status")).toHaveTextContent(
      "Rolling back analytics in prod to 4f9d2a7…",
    );
    expect(server.sent).toHaveLength(1);
    expect(server.sent[0]?.headers["x-lakana-background"]).toBe("1");
    await wait(2_000);
    expect(server.sent).toHaveLength(2);
    await wait(2_000);
    expect(server.sent).toHaveLength(3);
  });

  it("says it rolled back and stops reading", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { server, onEnded } = renderNote([{ status: "running" }, { status: "succeeded" }]);
    await settle();
    await wait(2_000);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Rolled back analytics in prod to 4f9d2a7.",
    );
    expect(onEnded).toHaveBeenCalledTimes(1);
    await wait(20_000);
    expect(server.sent).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("follows a rollback to its failure and reads the list again", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { server, onEnded } = renderNote([
      { status: "running" },
      {
        status: "failed",
        error_message: "lakana.yaml at 4f9d2a7 is not valid‮\nTraceback (most recent call last)",
      },
    ]);
    await settle();
    await wait(2_000);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "The rollback failed: lakana.yaml at 4f9d2a7 is not validU+202E",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("Traceback");
    expect(onEnded).toHaveBeenCalledTimes(1);
    await wait(10_000);
    expect(server.sent).toHaveLength(2);
  });

  it("says a failure the server gave no reason for", async () => {
    renderNote([{ status: "failed", error_message: null }]);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The rollback failed without saying why.",
    );
  });

  it("says why it cannot read the rollback, with Try again", async () => {
    renderNote([
      { status: 503, body: { type: "Unavailable", detail: "The server is busy." } },
      { status: "succeeded" },
    ]);

    expect(await screen.findByRole("alert")).toHaveTextContent("The server is busy.");
    screen.getByRole("button", { name: "Try again" }).click();
    expect(
      await screen.findByText("Rolled back analytics in prod to 4f9d2a7."),
    ).toBeInTheDocument();
  });

  it("does not read while the tab is hidden", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const { server } = renderNote([{ status: "running" }]);
    await settle();
    await wait(10_000);
    expect(server.sent).toHaveLength(0);

    visibility.mockReturnValue("visible");
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await settle();
    expect(server.sent).toHaveLength(1);
  });
});
