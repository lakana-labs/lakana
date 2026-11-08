import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Notices } from "./Notices";
import { SessionContext } from "./SessionProvider";

import { fakeServer } from "../test/server";
import { signedIn } from "../test/session";

const AT = "2026-11-08T10:00:00Z";

/**
 * Renders the notices with a page heading after them.
 *
 * @returns A user-event session.
 */
function renderNotices(): ReturnType<typeof userEvent.setup> {
  const user = userEvent.setup();
  render(
    <SessionContext value={signedIn()}>
      <main className="shell-main">
        <Notices />
        <h1 tabIndex={-1}>Home</h1>
      </main>
    </SessionContext>,
  );
  return user;
}

describe("Notices", () => {
  it("names each sign-in event, about the person or someone else", async () => {
    fakeServer({
      "GET /api/v1/sign-in/notices": {
        status: 200,
        body: [
          { id: "1", kind: "recovery_used", subject: "admin", created_at: AT },
          { id: "2", kind: "recovery_used", subject: "kim", created_at: AT },
          { id: "3", kind: "passkey_disabled", subject: "admin", created_at: AT },
          { id: "4", kind: "passkey_added", subject: "admin", created_at: AT },
          { id: "5", kind: "something_new", subject: null, created_at: AT },
        ],
      },
    });
    renderNotices();

    const region = await screen.findByRole("region", { name: "Sign-in notices" });
    const texts = within(region)
      .getAllByRole("listitem")
      .map((item) => item.querySelector("p")?.textContent);
    expect(texts).toHaveLength(5);
    expect(texts[0]).toMatch(/^A recovery link was used on your account on .+2026/);
    expect(texts[1]).toMatch(/^A recovery link was used on the account of kim on /);
    expect(texts[2]).toMatch(/^One of your passkeys was disabled on /);
    expect(texts[3]).toMatch(/^A passkey was added to your account on /);
    expect(texts[4]).toMatch(/^Something changed about signing in to your account on /);
  });

  it("dismisses a notice and moves focus on", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/notices": {
        status: 200,
        body: [
          { id: "1", kind: "passkey_added", subject: "admin", created_at: AT },
          { id: "2", kind: "recovery_used", subject: "kim", created_at: AT },
        ],
      },
      "DELETE /api/v1/sign-in/notices/1": { status: 204 },
      "DELETE /api/v1/sign-in/notices/2": { status: 204 },
    });
    const user = renderNotices();
    await screen.findByRole("region", { name: "Sign-in notices" });

    await user.click(screen.getAllByRole("button", { name: "Dismiss" })[0] as HTMLElement);

    expect(await screen.findAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Dismiss" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Home" })).toHaveFocus();
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toContain(
      "DELETE /api/v1/sign-in/notices/2",
    );
  });

  it("says why a notice could not be dismissed", async () => {
    fakeServer({
      "GET /api/v1/sign-in/notices": {
        status: 200,
        body: [{ id: "1", kind: "passkey_added", subject: "admin", created_at: AT }],
      },
      "DELETE /api/v1/sign-in/notices/1": {
        status: 404,
        body: { detail: "No such notice.", type: "NotFoundError" },
      },
    });
    const user = renderNotices();

    await user.click(await screen.findByRole("button", { name: "Dismiss" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The notice could not be dismissed: No such notice.",
    );
  });

  it("shows nothing without notices or when they cannot be read", async () => {
    const server = fakeServer({
      "GET /api/v1/sign-in/notices": { status: 503, body: { detail: "Busy.", type: "Busy" } },
    });
    renderNotices();

    await screen.findByRole("heading", { name: "Home" });
    expect(server.sent).toHaveLength(1);
    expect(screen.queryByRole("region")).not.toBeInTheDocument();
  });
});
