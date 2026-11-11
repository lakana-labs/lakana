import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { InterfaceStateProvider, useInterfaceState } from "./InterfaceState";

import { STATE } from "../test/home";
import { fakeServer } from "../test/server";

/**
 * Shows what the provider holds.
 *
 * @returns The state and the version as text.
 */
function Probe(): React.JSX.Element {
  const { state, version } = useInterfaceState();
  return <p>{`${JSON.stringify(state)} ${String(version)}`}</p>;
}

describe("InterfaceState", () => {
  it("reads the person's state and the version once", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/kim/interface-state": { status: 200, body: STATE },
      "GET /api/v1/info": { status: 200, body: { version: "0.23.0" } },
    });

    render(
      <InterfaceStateProvider name="kim">
        <Probe />
      </InterfaceStateProvider>,
    );

    expect(await screen.findByText(`${JSON.stringify(STATE)} 0.23.0`)).toBeInTheDocument();
    expect(server.sent.map((request) => request.path).sort()).toEqual([
      "/api/v1/info",
      "/api/v1/principals/kim/interface-state",
    ]);
  });

  it("holds nothing when the server cannot answer", async () => {
    const server = fakeServer({});

    render(
      <InterfaceStateProvider name="kim">
        <Probe />
      </InterfaceStateProvider>,
    );

    await vi.waitFor(() => {
      expect(server.sent).toHaveLength(2);
    });
    expect(screen.getByText("null null")).toBeInTheDocument();
  });

  it("drops answers that arrive after it is gone", async () => {
    const server = fakeServer({
      "GET /api/v1/principals/kim/interface-state": { status: 200, body: STATE },
      "GET /api/v1/info": { status: 200, body: { version: "0.23.0" } },
    });
    const error = vi.spyOn(console, "error");

    const { unmount } = render(
      <InterfaceStateProvider name="kim">
        <Probe />
      </InterfaceStateProvider>,
    );
    unmount();
    await vi.waitFor(() => {
      expect(server.sent).toHaveLength(2);
    });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });

  it("is used inside its provider only", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    expect(() => render(<Probe />)).toThrow("useInterfaceState needs an InterfaceStateProvider");
  });
});
