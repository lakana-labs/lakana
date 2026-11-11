import { act, render, screen } from "@testing-library/react";
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

  it("drops an answer for a person it no longer shows", async () => {
    let answerKim: () => void = () => undefined;
    const kimAnswered = new Promise<void>((resolve) => {
      answerKim = resolve;
    });
    const late = async (body: unknown): Promise<{ status: number; body: unknown }> => {
      await kimAnswered;
      return { status: 200, body };
    };
    const sam = { ...STATE, get_started_hidden: true };
    const server = fakeServer({
      "GET /api/v1/principals/kim/interface-state": () => late(STATE),
      "GET /api/v1/principals/sam/interface-state": { status: 200, body: sam },
      "GET /api/v1/info": () => late({ version: "0.22.0" }),
    });

    const { rerender } = render(
      <InterfaceStateProvider name="kim">
        <Probe />
      </InterfaceStateProvider>,
    );
    rerender(
      <InterfaceStateProvider name="sam">
        <Probe />
      </InterfaceStateProvider>,
    );
    expect(await screen.findByText(`${JSON.stringify(sam)} null`)).toBeVisible();
    await act(async () => {
      answerKim();
      await kimAnswered;
    });
    await vi.waitFor(() => {
      expect(server.sent).toHaveLength(4);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getByText(`${JSON.stringify(sam)} 0.22.0`)).toBeVisible();
  });

  it("is used inside its provider only", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    expect(() => render(<Probe />)).toThrow("useInterfaceState needs an InterfaceStateProvider");
  });
});
