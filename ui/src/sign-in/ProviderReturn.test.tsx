import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type JSX, useState } from "react";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import { PROVIDER_TRIP_KEY } from "../api/oidc";
import { ProviderConfirmed, ProviderReturn } from "./ProviderReturn";
import { ProviderNote } from "./ProviderNote";
import { SessionContext, type SessionValue } from "./SessionProvider";

import { held } from "../test/pending";
import { fakeServer, type FakeRoute } from "../test/server";
import { signedIn } from "../test/session";

const OKTA = { status: 200, body: { enabled: true, display_name: "Okta" } };

/**
 * A refusal of the provider's sign-in.
 *
 * @param reason - The reason.
 * @returns The route's answer.
 */
const refusal = (reason: string) => ({
  status: 403,
  body: { detail: `The server's words for ${reason}.`, type: "OidcSignInRefused", reason },
});

/**
 * A page of the interface: its heading and the note a confirmation leaves.
 *
 * @param props - The heading.
 * @param props.title - The heading.
 * @returns The page.
 */
function Landing({ title }: { readonly title: string }): JSX.Element {
  const location = useLocation();
  return (
    <main>
      <ProviderNote />
      <h1 tabIndex={-1}>{title}</h1>
      <p>{location.search}</p>
    </main>
  );
}

/**
 * The session around the pages: signed out until `refresh` signs the person in.
 *
 * @param props - The router and the first state.
 * @param props.router - The router.
 * @param props.start - The session value to start with.
 * @returns The pages.
 */
function Session({
  router,
  start,
}: {
  readonly router: ReturnType<typeof createMemoryRouter>;
  readonly start: SessionValue;
}): JSX.Element {
  const [signedInNow, setSignedIn] = useState(false);
  const value: SessionValue = signedInNow
    ? signedIn()
    : {
        ...start,
        refresh: () => {
          setSignedIn(true);
          return Promise.resolve();
        },
      };
  return (
    <SessionContext value={value}>
      <RouterProvider router={router} />
    </SessionContext>
  );
}

/**
 * Renders a return page at its address, with the pages it may open.
 *
 * @param path - Where the provider sent the browser.
 * @param routes - The fake server's routes.
 * @param start - The session value to start with.
 * @returns The router and a user-event session.
 */
function renderAt(path: string, routes: Record<string, FakeRoute>, start: SessionValue) {
  const server = fakeServer(routes);
  const router = createMemoryRouter(
    [
      { path: "/sign-in/oidc", element: <ProviderReturn /> },
      { path: "/sign-in/oidc/confirmed", element: <ProviderConfirmed /> },
      { path: "/sign-in", element: <Landing title="Sign in to Lakana" /> },
      { path: "/catalog/prod", element: <Landing title="Catalog" /> },
      { path: "/settings/access/grants", element: <Landing title="Grants" /> },
      { path: "/account", element: <Landing title="Passkeys and sessions" /> },
      { path: "/", element: <Landing title="Home" /> },
    ],
    { initialEntries: ["/", path], initialIndex: 1 },
  );
  const user = userEvent.setup();
  const view = render(<Session router={router} start={start} />);
  return { router, server, user, view };
}

const SIGNED_OUT = signedIn({ state: "signed-out", session: undefined });

const REASONS: readonly (readonly [string, RegExp])[] = [
  ["not_linked", /Ask an administrator for an invite\.$/],
  ["deactivated", /deactivated\. Ask an administrator to reactivate it\.$/],
  ["provider_unreachable", /^Lakana could not reach the identity provider\. Try again/],
  ["provider_refused", /did not sign you in\. Try again, or ask your IT department\.$/],
  ["token_invalid", /Try again; if it keeps happening, tell an administrator\.$/],
  ["flow_expired", /Start again\.$/],
  ["identity_taken", /already linked to another Lakana account\. Ask an administrator\.$/],
  ["identity_mismatch", /as someone else\. Sign in there as yourself and try again\.$/],
  ["link_unusable", /used or withdrawn, or it expired/],
  ["last_unlinked_admin", /Another administrator must keep a passkey/],
  ["cannot_link_operator", /server operator's account cannot sign in through/],
  ["already_linked", /already linked to the identity provider\.$/],
  ["auth_too_old", /confirmed an earlier sign-in, not a new one/],
];

describe("ProviderReturn", () => {
  it("finishes and lands where the person was going", async () => {
    const { router, server } = renderAt(
      "/sign-in/oidc",
      {
        "POST /api/v1/sign-in/oidc/finish": {
          status: 200,
          body: { name: "kim", csrf_token: "c-1", return_to: "/catalog/prod" },
        },
      },
      SIGNED_OUT,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "Catalog" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/catalog/prod");
    expect(router.state.historyAction).toBe("REPLACE");
    expect(server.sent.map((request) => request.path)).toEqual(["/api/v1/sign-in/oidc/finish"]);
  });

  it("says it signs the person in while it waits, and opens Home for a foreign path", async () => {
    const { router } = renderAt(
      "/sign-in/oidc",
      {
        "POST /api/v1/sign-in/oidc/finish": {
          status: 200,
          body: { name: "kim", csrf_token: "c-1", return_to: "//evil.example/x" },
        },
      },
      SIGNED_OUT,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Signing you in…");
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it.each(REASONS)("shows the sentence for each refusal: %s", async (reason, sentence) => {
    renderAt("/sign-in/oidc", { "POST /api/v1/sign-in/oidc/finish": refusal(reason) }, SIGNED_OUT);

    const heading = await screen.findByRole("heading", {
      level: 1,
      name: "You were not signed in",
    });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(screen.getByText(sentence)).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("falls back to the server's sentence for an unknown reason", async () => {
    renderAt(
      "/sign-in/oidc",
      { "POST /api/v1/sign-in/oidc/finish": refusal("something_new") },
      SIGNED_OUT,
    );

    expect(await screen.findByText("The server's words for something_new.")).toBeInTheDocument();
  });

  it("explains the last unlinked administrator, and Try again goes to sign-in", async () => {
    const { router, user } = renderAt(
      "/sign-in/oidc",
      { "POST /api/v1/sign-in/oidc/finish": refusal("last_unlinked_admin") },
      SIGNED_OUT,
    );

    expect(
      await screen.findByText(/^You are the last administrator who signs in with a passkey\./),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(router.state.location.pathname).toBe("/sign-in");
  });
});

describe("finishing once per page load", () => {
  afterEach(() => {
    sessionStorage.clear();
  });

  it("a page shown again reads the first finish instead of finishing twice", async () => {
    const request = held();
    const { server, view } = renderAt(
      "/sign-in/oidc",
      { "POST /api/v1/sign-in/oidc/finish": request.route },
      SIGNED_OUT,
    );
    view.unmount();
    request.release(refusal("flow_expired"));
    await waitFor(() => {
      expect(server.sent).toHaveLength(1);
    });

    renderAt("/sign-in/oidc", {}, SIGNED_OUT);

    expect(await screen.findByText(/Start again\.$/)).toBeInTheDocument();
  });

  it("a confirmation shown again reads the first finish too", async () => {
    const request = held();
    const { server, view } = renderAt(
      "/sign-in/oidc/confirmed",
      { "POST /api/v1/sign-in/oidc/finish-in-session": request.route },
      signedIn(),
    );
    view.unmount();
    request.release(refusal("identity_mismatch"));
    await waitFor(() => {
      expect(server.sent.length).toBeGreaterThan(0);
    });

    renderAt("/sign-in/oidc/confirmed", {}, signedIn());

    expect(await screen.findByText(/as someone else/)).toBeInTheDocument();
  });

  it("a confirmation the tab did not start says the step-up's note", async () => {
    renderAt(
      "/sign-in/oidc/confirmed",
      {
        "POST /api/v1/sign-in/oidc/finish-in-session": {
          status: 200,
          body: { return_to: "/account", step_up_until: null },
        },
        "GET /api/v1/sign-in/oidc": { status: 200, body: { enabled: true, display_name: null } },
      },
      signedIn(),
    );

    expect(
      await screen.findByText(
        "Confirmed with the identity provider. Do it again now; it won't ask for 15 minutes.",
      ),
    ).toBeInTheDocument();
  });
});

describe("ProviderConfirmed", () => {
  afterEach(() => {
    sessionStorage.clear();
  });

  it("confirmed: returns to the page and says to press again", async () => {
    sessionStorage.setItem(
      PROVIDER_TRIP_KEY,
      JSON.stringify({ purpose: "step_up", back: "/settings/access/grants?who=kim" }),
    );
    const { router } = renderAt(
      "/sign-in/oidc/confirmed",
      {
        "POST /api/v1/sign-in/oidc/finish-in-session": {
          status: 200,
          body: {
            return_to: "/settings/access/grants?who=kim",
            step_up_until: "2026-11-25T10:15Z",
          },
        },
        "GET /api/v1/sign-in/oidc": OKTA,
      },
      signedIn(),
    );

    const note = await screen.findByText(
      "Confirmed with Okta. Do it again now; it won't ask for 15 minutes.",
    );
    expect(router.state.location.pathname).toBe("/settings/access/grants");
    expect(router.state.location.search).toBe("?who=kim");
    expect(router.state.historyAction).toBe("REPLACE");
    await waitFor(() => {
      expect(note).toHaveFocus();
    });
    expect(sessionStorage.getItem(PROVIDER_TRIP_KEY)).toBeNull();
  });

  it("says a link from the profile is done, without the provider's name when it cannot be read", async () => {
    sessionStorage.setItem(
      PROVIDER_TRIP_KEY,
      JSON.stringify({ purpose: "link", back: "/account" }),
    );
    renderAt(
      "/sign-in/oidc/confirmed",
      {
        "POST /api/v1/sign-in/oidc/finish-in-session": {
          status: 200,
          body: { return_to: "/account", step_up_until: null },
        },
      },
      signedIn(),
    );

    expect(
      await screen.findByText(
        "You now sign in through the identity provider. Your passkeys are kept but no longer sign you in.",
      ),
    ).toBeInTheDocument();
  });

  it("names a refusal, and Try again goes back to where it started", async () => {
    sessionStorage.setItem(
      PROVIDER_TRIP_KEY,
      JSON.stringify({ purpose: "link", back: "/account" }),
    );
    const { router, user } = renderAt(
      "/sign-in/oidc/confirmed",
      {
        "POST /api/v1/sign-in/oidc/finish-in-session": refusal("identity_taken"),
        "GET /api/v1/sign-in/oidc": OKTA,
      },
      signedIn(),
    );

    const heading = await screen.findByRole("heading", {
      level: 1,
      name: "This was not confirmed",
    });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(screen.getByText(/already linked to another Lakana account/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(router.state.location.pathname).toBe("/account");
  });

  it("goes back Home after a refusal when the tab remembers no start", async () => {
    const { router, user } = renderAt(
      "/sign-in/oidc/confirmed",
      { "POST /api/v1/sign-in/oidc/finish-in-session": refusal("auth_too_old") },
      signedIn(),
    );

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(router.state.location.pathname).toBe("/");
  });
});
