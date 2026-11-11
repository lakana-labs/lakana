import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { InterfaceStateProvider } from "../shell/InterfaceState";
import { STATE } from "../test/home";
import { fakeServer } from "../test/server";
import { TourProvider, useTour } from "./TourProvider";

/**
 * Starts the tour and says whether it runs.
 *
 * @returns A button and the state as text.
 */
function Probe(): React.JSX.Element {
  const tour = useTour();
  return (
    <button
      type="button"
      onClick={() => {
        tour.start();
      }}
    >
      {tour.active ? "running" : "resting"}
    </button>
  );
}

/**
 * Renders the provider for kim, with a probe inside.
 */
function renderProvider(): void {
  render(
    <InterfaceStateProvider name="kim">
      <TourProvider areas={[]}>
        <Probe />
      </TourProvider>
    </InterfaceStateProvider>,
  );
}

describe("TourProvider", () => {
  it("says whether the tour runs", async () => {
    fakeServer({
      "GET /api/v1/principals/kim/interface-state": { status: 200, body: STATE },
      "GET /api/v1/info": { status: 200, body: { version: "0.23.0" } },
      "GET /api/v1/home/get-started": { status: 503, body: { detail: "down" } },
    });
    const user = userEvent.setup();
    render(
      <InterfaceStateProvider name="kim">
        <TourProvider areas={[]}>
          <Probe />
        </TourProvider>
      </InterfaceStateProvider>,
    );

    await user.click(screen.getByRole("button", { name: "resting" }));
    const step = await screen.findByRole("dialog", { name: "Search" });
    expect(screen.getByRole("button", { name: "running" })).toBeInTheDocument();

    await user.click(within(step).getByRole("button", { name: "Skip tour" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "resting" })).toHaveFocus();
    });
  });

  it("waits with the welcome until no dialog is open", async () => {
    fakeServer({
      "GET /api/v1/principals/kim/interface-state": {
        status: 200,
        body: { ...STATE, tour_seen: false },
      },
      "GET /api/v1/info": { status: 200, body: { version: "0.23.0" } },
      "GET /api/v1/home/get-started": { status: 503, body: { detail: "down" } },
    });
    // A dialog the person opened before their state arrived, such as Profile picture.
    const open = document.createElement("div");
    open.setAttribute("role", "dialog");
    document.body.append(open);
    renderProvider();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "resting" })).toBeInTheDocument();
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole("dialog", { name: "Welcome to Lakana" })).not.toBeInTheDocument();

    open.remove();

    expect(await screen.findByRole("dialog", { name: "Welcome to Lakana" })).toBeInTheDocument();
  });

  it("shows no welcome once the person started the tour while a dialog was open", async () => {
    fakeServer({
      "GET /api/v1/principals/kim/interface-state": {
        status: 200,
        body: { ...STATE, tour_seen: false },
      },
      "GET /api/v1/info": { status: 200, body: { version: "0.23.0" } },
      "GET /api/v1/home/get-started": { status: 503, body: { detail: "down" } },
    });
    const open = document.createElement("div");
    open.setAttribute("role", "dialog");
    document.body.append(open);
    const user = userEvent.setup();
    renderProvider();
    await new Promise((resolve) => setTimeout(resolve, 50));

    await user.click(screen.getByRole("button", { name: "resting" }));
    open.remove();

    const step = await screen.findByRole("dialog", { name: "Search" });
    await user.click(within(step).getByRole("button", { name: "Skip tour" }));

    // No dialog is open any more, and the person has seen the tour: no welcome after it.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "resting" })).toBeInTheDocument();
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("needs its provider", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    expect(() => render(<Probe />)).toThrow("useTour needs a TourProvider");

    vi.restoreAllMocks();
  });
});
