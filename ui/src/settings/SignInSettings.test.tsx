import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { freshSession, STALE_SESSION, STEP_UP_ROUTES } from "../test/access";
import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const SETTINGS = {
  idle_timeout_minutes: 720,
  absolute_lifetime_minutes: 10080,
  bounds: {
    idle_timeout_minutes_min: 15,
    idle_timeout_minutes_max: 1440,
    absolute_lifetime_minutes_min: 60,
    absolute_lifetime_minutes_max: 43200,
  },
};

const OKTA = {
  display_name: "Okta",
  issuer: "https://example.okta.com/oauth2/default",
  linked_people: 12,
  unlinked_admins: 2,
};
const WITH_PROVIDER = {
  ...SETTINGS,
  bounds: {
    ...SETTINGS.bounds,
    token_provider_window_days_min: 1,
    token_provider_window_days_max: 365,
  },
  token_provider_window_days: 30,
  provider: OKTA,
};

/**
 * Sign-in's routes, with a stale passkey touch and a PATCH that answers what it was sent.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function staleRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
    "GET /api/v1/sign-in/session": STALE_SESSION,
    ...STEP_UP_ROUTES,
    "PATCH /api/v1/settings/sign-in": (request) => ({
      status: 200,
      body: { ...SETTINGS, ...(request.body as object) },
    }),
    ...more,
  };
}

/**
 * Types new lengths into both fields.
 *
 * @param user - The user-event session.
 * @param idle - The idle timeout to type.
 * @param absolute - The longest session to type.
 */
async function typeLengths(
  user: ReturnType<typeof renderShell>["user"],
  idle: string,
  absolute: string,
): Promise<void> {
  const idleField = await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
  await user.clear(idleField);
  await user.type(idleField, idle);
  const absoluteField = screen.getByRole("textbox", { name: "Longest session in minutes" });
  await user.clear(absoluteField);
  await user.type(absoluteField, absolute);
}

describe("SignInSettings", () => {
  it("shows the session lengths in their own section", async () => {
    renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } },
    });

    const section = await screen.findByRole("region", { name: "Session lengths" });
    expect(within(section).getByRole("heading", { level: 2 })).toHaveTextContent("Session lengths");
    expect(
      await within(section).findByRole("textbox", { name: "Idle timeout in minutes" }),
    ).toBeVisible();
    expect(
      within(section).getByRole("textbox", { name: "Longest session in minutes" }),
    ).toBeVisible();
    expect(within(section).getByRole("button", { name: "Save" })).toBeVisible();
    expect(section).not.toContainElement(screen.getByText(/How long a browser session lasts/));
  });

  it("asks for the touch before saving", async () => {
    const webauthn = fakeWebAuthn();
    const { user, server } = renderShell("/settings/sign-in", { routes: staleRoutes() });
    await typeLengths(user, "60", "1440");
    const from = server.sent.length;

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved. New sessions use these lengths.")).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(server.sent.slice(from).map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "PATCH /api/v1/settings/sign-in",
    ]);
    expect(server.sent.at(-1)?.body).toEqual({
      idle_timeout_minutes: 60,
      absolute_lifetime_minutes: 1440,
    });
  });

  it("a cancelled touch saves nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const { user, server } = renderShell("/settings/sign-in", { routes: staleRoutes() });
    await typeLengths(user, "60", "1440");

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The passkey request was cancelled or took too long. Try again when you are ready.",
    );
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
    expect(screen.getByRole("textbox", { name: "Idle timeout in minutes" })).toHaveValue("60");
    expect(screen.getByRole("textbox", { name: "Longest session in minutes" })).toHaveValue("1440");
    expect(screen.queryByText("Saved. New sessions use these lengths.")).not.toBeInTheDocument();
  });

  it("an account without a passkey hears the next step", async () => {
    const { user, server } = renderShell("/settings/sign-in", {
      routes: staleRoutes({
        "POST /api/v1/sign-in/step-up/options": {
          status: 409,
          body: { type: "NoPasskeyError", detail: "Your account has no passkey at this address." },
        },
      }),
    });
    await typeLengths(user, "60", "1440");

    await user.click(screen.getByRole("button", { name: "Save" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Your account has no passkey yet, and this action needs a passkey touch. Add one first " +
        "under Passkeys and sessions, in the menu under your name, then try again.",
    );
    expect(within(alert).getByRole("link", { name: "Add a passkey" })).toHaveAttribute(
      "href",
      "/account",
    );
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("keeps the focus on Save after saving", async () => {
    fakeWebAuthn();
    const { user } = renderShell("/settings/sign-in", { routes: staleRoutes() });
    await typeLengths(user, "60", "1440");
    const save = screen.getByRole("button", { name: "Save" });

    await user.click(save);

    expect(await screen.findByText("Saved. New sessions use these lengths.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Save" })).toHaveFocus();
    });
    expect(screen.getByRole("textbox", { name: "Idle timeout in minutes" })).toHaveValue("60");
  });

  it("shows the session lengths with their bounds, edited in place", async () => {
    renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } },
    });

    const idle = await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
    expect(idle).toHaveValue("720");
    expect(idle).toHaveAccessibleDescription(/Between 15 and 1,440 minutes/);
    const absolute = screen.getByRole("textbox", { name: "Longest session in minutes" });
    expect(absolute).toHaveValue("10080");
    expect(absolute).toHaveAccessibleDescription(/Between 60 and 43,200 minutes/);
    expect(screen.getByText(/applies to sessions that begin afterwards/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Sign-in" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign-in" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("row")).not.toBeInTheDocument();
  });

  it.each([
    ["Idle timeout in minutes", "10", "The idle timeout is between 15 and 1,440 minutes."],
    ["Idle timeout in minutes", "abc", "The idle timeout is between 15 and 1,440 minutes."],
    ["Idle timeout in minutes", "2000", "The idle timeout is between 15 and 1,440 minutes."],
    ["Longest session in minutes", "x", "The longest session is between 60 and 43,200 minutes."],
    ["Longest session in minutes", "30", "The longest session is between 60 and 43,200 minutes."],
    [
      "Longest session in minutes",
      "50000",
      "The longest session is between 60 and 43,200 minutes.",
    ],
  ])("refuses an out-of-bound %s (%s) and names the bound", async (field, value, message) => {
    const { user, server } = renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } },
    });

    const input = await screen.findByRole("textbox", { name: field });
    await user.clear(input);
    await user.type(input, value);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("refuses a longest session shorter than the idle timeout", async () => {
    const { user } = renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS } },
    });

    const absolute = await screen.findByRole("textbox", { name: "Longest session in minutes" });
    await user.clear(absolute);
    await user.type(absolute, "600");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The longest session cannot be shorter than the idle timeout (720 minutes).",
    );
  });

  it("saves new lengths and says so", async () => {
    const { user, server } = renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
        "GET /api/v1/sign-in/session": freshSession(),
        "PATCH /api/v1/settings/sign-in": (request) => ({
          status: 200,
          body: { ...SETTINGS, ...(request.body as object) },
        }),
      },
    });

    const idle = await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
    await user.clear(idle);
    await user.type(idle, "60");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved. New sessions use these lengths.")).toBeInTheDocument();
    expect(server.sent.at(-1)?.body).toEqual({
      idle_timeout_minutes: 60,
      absolute_lifetime_minutes: 10080,
    });
  });

  it("shows the pending sign on Save while the lengths are saved", async () => {
    const request = held();
    const { user } = renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
        "GET /api/v1/sign-in/session": freshSession(),
        "PATCH /api/v1/settings/sign-in": request.route,
      },
    });
    await screen.findByRole("textbox", { name: "Idle timeout in minutes" });

    await user.click(screen.getByRole("button", { name: "Save" }));

    expectPending(screen.getByRole("button", { name: /^Save/ }));
    request.release({ status: 200, body: SETTINGS });
    expect(await screen.findByText("Saved. New sessions use these lengths.")).toBeInTheDocument();
  });

  it("names the server's reason when saving is refused", async () => {
    const { user } = renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": { status: 200, body: SETTINGS },
        "GET /api/v1/sign-in/session": freshSession(),
        "PATCH /api/v1/settings/sign-in": {
          status: 403,
          body: { type: "PermissionDenied", detail: "You need the ADMIN role." },
        },
      },
    });

    await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("You need the ADMIN role.");
  });

  it("names the reason when the settings cannot be read", async () => {
    renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": {
          status: 403,
          body: { type: "PermissionDenied", detail: "You need the ADMIN role." },
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("You need the ADMIN role.");
  });

  it("shows the provider, issuer and counts read-only", async () => {
    renderShell("/settings/sign-in", {
      routes: { "GET /api/v1/settings/sign-in": { status: 200, body: WITH_PROVIDER } },
    });

    const section = await screen.findByRole("region", { name: "Single sign-on" });
    const facts = within(section)
      .getAllByRole("term")
      .map((term) => [term.textContent, term.nextElementSibling?.textContent]);
    expect(facts).toEqual([
      ["Provider", "Okta"],
      ["Issuer", "https://example.okta.com/oauth2/default"],
      ["Linked people", "12"],
      ["Unlinked administrators", "2"],
    ]);
    expect(within(section).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(section).queryByRole("button")).not.toBeInTheDocument();
    expect(within(section).queryByText(/^Warning:/)).not.toBeInTheDocument();
    const order = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(order).toEqual(["Single sign-on", "Session lengths"]);
  });

  it.each([
    [1, "Only one administrator can still sign in with a passkey. Keep at least one"],
    [0, "No administrator can sign in with a passkey."],
  ])("warns when %i unlinked administrators are left", async (left, words) => {
    renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": {
          status: 200,
          body: { ...WITH_PROVIDER, provider: { ...OKTA, unlinked_admins: left } },
        },
      },
    });

    const section = await screen.findByRole("region", { name: "Single sign-on" });
    const warning = within(section)
      .getByText(/^Warning:/)
      .closest("p") as HTMLElement;
    expect(warning).toHaveTextContent(words);
    expect(warning.querySelector("svg")).not.toBeNull();
  });

  it("saves the token window with the session lengths after the touch", async () => {
    const webauthn = fakeWebAuthn();
    const { user, server } = renderShell("/settings/sign-in", {
      routes: staleRoutes({
        "GET /api/v1/settings/sign-in": { status: 200, body: WITH_PROVIDER },
        "PATCH /api/v1/settings/sign-in": (request) => ({
          status: 200,
          body: { ...WITH_PROVIDER, ...(request.body as object) },
        }),
      }),
    });
    const field = await screen.findByRole("textbox", { name: "Token window in days" });
    expect(field).toHaveValue("30");
    await user.clear(field);
    await user.type(field, "90");
    const from = server.sent.length;

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved. New sessions use these lengths.")).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(server.sent.slice(from).map((request) => request.method)).toEqual([
      "GET",
      "POST",
      "POST",
      "PATCH",
    ]);
    expect(server.sent.at(-1)?.body).toEqual({
      idle_timeout_minutes: 720,
      absolute_lifetime_minutes: 10080,
      token_provider_window_days: 90,
    });
    expect(screen.getByRole("textbox", { name: "Token window in days" })).toHaveValue("90");
  });

  it.each(["0", "366", "thirty"])("refuses %s with the bound in words", async (typed) => {
    const { user, server } = renderShell("/settings/sign-in", {
      routes: staleRoutes({ "GET /api/v1/settings/sign-in": { status: 200, body: WITH_PROVIDER } }),
    });
    const field = await screen.findByRole("textbox", { name: "Token window in days" });
    await user.clear(field);
    await user.type(field, typed);

    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The token window is between 1 and 365 days. Enter a whole number in that range.",
    );
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("hides single sign-on when it is off", async () => {
    const webauthn = fakeWebAuthn();
    const { user, server } = renderShell("/settings/sign-in", {
      routes: staleRoutes({
        "GET /api/v1/settings/sign-in": {
          status: 200,
          body: { ...SETTINGS, token_provider_window_days: 30, provider: null },
        },
      }),
    });

    await screen.findByRole("textbox", { name: "Idle timeout in minutes" });
    expect(screen.queryByRole("region", { name: "Single sign-on" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Token window in days" })).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/provider|token window/i);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Saved. New sessions use these lengths.");
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(server.sent.at(-1)?.body).not.toHaveProperty("token_provider_window_days");
  });

  it("a 255-character issuer wraps in its cell", async () => {
    const issuer = `https://${"a".repeat(240)}.example/x`;
    renderShell("/settings/sign-in", {
      routes: {
        "GET /api/v1/settings/sign-in": {
          status: 200,
          body: { ...WITH_PROVIDER, provider: { ...OKTA, issuer } },
        },
      },
    });

    const section = await screen.findByRole("region", { name: "Single sign-on" });
    const code = within(section).getByText((_, element) => element?.tagName === "CODE");
    expect(code).toHaveTextContent(issuer);
    expect(code).toHaveClass("sso-issuer");
    expect(code.closest("dd")).not.toBeNull();
  });
});
