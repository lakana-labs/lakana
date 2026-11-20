import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { CliDevicePage, CliSignInPage } from "./CliSignIn";
import { SessionContext } from "./SessionProvider";

import { send, setStepUp } from "../api/client";
import { ADMIN_SESSION, type FakeRoute, fakeServer } from "../test/server";
import { signedIn } from "../test/session";

const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
const FRESH = {
  status: 200,
  body: { ...ADMIN_SESSION, name: "kim", step_up_until: "2999-01-01T00:00:00Z" },
};
const STALE = { status: 200, body: { ...ADMIN_SESSION, name: "kim", step_up_until: null } };

/**
 * Renders a page at an address, signed in as kim.
 *
 * @param element - The page.
 * @param at - The address.
 * @returns A user-event session.
 */
function renderAt(element: ReactNode, at: string): ReturnType<typeof userEvent.setup> {
  const router = createMemoryRouter([{ path: "*", element }], { initialEntries: [at] });
  const user = userEvent.setup();
  render(
    <SessionContext value={signedIn({ session: { ...ADMIN_SESSION, name: "kim" } })}>
      <RouterProvider router={router} />
    </SessionContext>,
  );
  return user;
}

/**
 * The address `lakana login` opens.
 *
 * @param overrides - Query parameters to change.
 * @returns The path and query.
 */
function link(overrides: Record<string, string> = {}): string {
  const query = new URLSearchParams({
    port: "49152",
    state: "st-1",
    challenge: CHALLENGE,
    host: "laptop-7",
    ...overrides,
  });
  return `/sign-in/cli?${query.toString()}`;
}

/**
 * The fake server of a confirmation.
 *
 * @param session - The session route's answer.
 * @param extra - More routes.
 * @returns The server.
 */
function server(session: FakeRoute = FRESH, extra: Record<string, FakeRoute> = {}) {
  return fakeServer({
    "GET /api/v1/sign-in/session": session,
    "POST /api/v1/sign-in/cli/approvals": { status: 200, body: { code: "c0de", port: 49152 } },
    ...extra,
  });
}

/**
 * The sent requests as "METHOD path".
 *
 * @param sent - The requests.
 * @returns One line per request.
 */
function lines(sent: readonly { method: string; path: string }[]): string[] {
  return sent.map((request) => `${request.method} ${request.path}`);
}

describe("CliSignInPage", () => {
  it("names the token about to be created", async () => {
    server();
    renderAt(<CliSignInPage navigate={vi.fn()} />, link());

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Create the API token “lakana login on laptop-7” for kim?",
      }),
    ).toHaveFocus();
    expect(
      screen.getByText("The command line on that computer can then do everything you can."),
    ).toBeInTheDocument();
    expect(document.title).toContain("Sign in the command line");
  });

  it("sends the browser to the loopback address with the code and state", async () => {
    const fake = server();
    const navigate = vi.fn();
    const user = renderAt(<CliSignInPage navigate={navigate} />, link());

    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(navigate).toHaveBeenCalledWith("http://127.0.0.1:49152/?code=c0de&state=st-1");
    expect(fake.sent.at(-1)?.body).toEqual({
      port: 49152,
      code_challenge: CHALLENGE,
      host: "laptop-7",
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/You can close this tab/);
  });

  it("asks for the touch before creating", async () => {
    const fake = server(STALE, {
      "POST /api/v1/sign-in/step-up/options": { status: 200, body: {} },
    });
    const touch = vi.fn(async () => {
      await send("POST", "/api/v1/sign-in/step-up/options", {});
    });
    setStepUp(touch);
    const user = renderAt(<CliSignInPage navigate={vi.fn()} />, link());

    await user.click(screen.getByRole("button", { name: "Create token" }));

    await vi.waitFor(() => {
      expect(lines(fake.sent)).toEqual([
        "GET /api/v1/sign-in/session",
        "POST /api/v1/sign-in/step-up/options",
        "POST /api/v1/sign-in/cli/approvals",
      ]);
    });
    expect(touch).toHaveBeenCalledTimes(1);
  });

  it("a cancelled touch sends nothing", async () => {
    const fake = server(STALE);
    setStepUp(() => Promise.reject(new DOMException("Cancelled.", "NotAllowedError")));
    const navigate = vi.fn();
    const user = renderAt(<CliSignInPage navigate={navigate} />, link());

    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(await screen.findByRole("alert")).not.toBeEmptyDOMElement();
    expect(lines(fake.sent)).toEqual(["GET /api/v1/sign-in/session"]);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("cancel tells the command line it was refused", async () => {
    const fake = server();
    const navigate = vi.fn();
    const user = renderAt(<CliSignInPage navigate={navigate} />, link());

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(navigate).toHaveBeenCalledWith("http://127.0.0.1:49152/?error=access_denied&state=st-1");
    expect(fake.sent).toHaveLength(0);
  });

  it("keeps the checked port when the server answers another", async () => {
    server(FRESH, {
      "POST /api/v1/sign-in/cli/approvals": { status: 200, body: { code: "c0de", port: 80 } },
    });
    const navigate = vi.fn();
    const user = renderAt(<CliSignInPage navigate={navigate} />, link());

    await user.click(screen.getByRole("button", { name: "Create token" }));

    await vi.waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("http://127.0.0.1:49152/?code=c0de&state=st-1");
    });
  });

  it.each([["0"], ["80"], ["1023"], ["65536"], ["99999"], ["8080/evil"], ["8080.5"], [""]])(
    "refuses a port outside 1024 to 65535 (%s)",
    (port) => {
      const fake = server();
      const navigate = vi.fn();
      renderAt(<CliSignInPage navigate={navigate} />, link({ port }));

      expect(
        screen.getByRole("heading", { level: 1, name: "This sign-in link does not work" }),
      ).toHaveFocus();
      expect(screen.getByText(/its port must be between 1024 and 65535/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Create token" })).toBeNull();
      expect(navigate).not.toHaveBeenCalled();
      expect(fake.sent).toHaveLength(0);
    },
  );

  it.each([
    [{ state: "x".repeat(5 * 1024) }],
    [{ state: "" }],
    [{ state: "a b" }],
    [{ challenge: "short" }],
    [{ host: "h".repeat(257) }],
  ])("refuses a changed link (%o)", (overrides) => {
    server();
    renderAt(<CliSignInPage navigate={vi.fn()} />, link(overrides));

    expect(
      screen.getByRole("heading", { level: 1, name: "This sign-in link does not work" }),
    ).toBeInTheDocument();
  });

  it("marks hidden characters in the host", () => {
    server();
    renderAt(<CliSignInPage navigate={vi.fn()} />, link({ host: "lap\u202etop\u200b7" }));

    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent(/U\+202E/);
    expect(heading).toHaveTextContent(/U\+200B/);
    expect(heading.textContent).not.toContain("\u202e");
  });
});

describe("CliDevicePage", () => {
  /**
   * The fake server of the device page.
   *
   * @param extra - Routes to add or change.
   * @returns The server.
   */
  const deviceServer = (extra: Record<string, FakeRoute> = {}) =>
    fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/sign-in/cli/device/describe": { status: 200, body: { host: "build-box" } },
      "POST /api/v1/sign-in/cli/device/approve": { status: 204 },
      "POST /api/v1/sign-in/cli/device/deny": { status: 204 },
      ...extra,
    });

  it("device: a wrong code says so and keeps the focus in the field", async () => {
    deviceServer({
      "POST /api/v1/sign-in/cli/device/describe": {
        status: 404,
        body: { detail: "No sign-in is waiting.", type: "NotFoundError" },
      },
    });
    const user = renderAt(<CliDevicePage />, "/sign-in/device");

    await user.type(screen.getByRole("textbox", { name: "Code" }), "BBBB-BBBB{Enter}");

    expect(
      await screen.findByText(/^No sign-in is waiting for this code\. Check it/),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Code" })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Code" })).toHaveAttribute("aria-invalid", "true");
  });

  it("device: too many wrong codes names the server's sentence", async () => {
    deviceServer({
      "POST /api/v1/sign-in/cli/device/describe": {
        status: 429,
        body: { detail: "Too many codes; wait 10 minutes.", type: "TooManyAttemptsError" },
      },
    });
    const user = renderAt(<CliDevicePage />, "/sign-in/device");

    await user.type(screen.getByRole("textbox", { name: "Code" }), "BBBB-BBBB{Enter}");

    expect(await screen.findByText(/wait 10 minutes/)).toBeInTheDocument();
  });

  it("device: approve then the page says to go back to the terminal", async () => {
    const fake = deviceServer();
    const user = renderAt(<CliDevicePage />, "/sign-in/device");

    await user.type(screen.getByRole("textbox", { name: "Code" }), " bcdf-ghjk {Enter}");
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Create the API token “lakana login on build-box” for kim?",
      }),
    ).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Create token" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Done. Go back to the terminal: lakana login finishes there.",
    );
    expect(fake.sent.at(-1)).toMatchObject({
      path: "/api/v1/sign-in/cli/device/approve",
      body: { user_code: "bcdf-ghjk" },
    });
  });

  it("device: cancel refuses the sign-in", async () => {
    const fake = deviceServer();
    const user = renderAt(<CliDevicePage />, "/sign-in/device");

    await user.type(screen.getByRole("textbox", { name: "Code" }), "BCDF-GHJK{Enter}");
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/^Refused\./);
    expect(fake.sent.at(-1)?.path).toBe("/api/v1/sign-in/cli/device/deny");
  });
});
