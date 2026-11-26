import { screen, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { areasFor } from "../shell/areas";
import { ciTrustRoutes, DEV_RULE, PROD_RULE } from "../test/ciTrust";
import { renderShell } from "../test/render";

const PATH = "/settings/ci-trust/explain";

/** The claims of a push to main, as GitHub writes them. */
const CLAIMS = {
  iss: "https://token.actions.githubusercontent.com",
  repository_id: "123456",
  ref: "refs/heads/main",
  event_name: "push",
  environment: "prod",
  sha: "0123456789abcdef0123456789abcdef01234567",
};

/** What the server answers for those claims. */
const REPORT = {
  issuer: CLAIMS.iss,
  environment: "prod",
  repository: "analytics",
  verdict: `accepted by rule ${PROD_RULE}`,
  matching: [PROD_RULE],
  rules: [
    {
      rule_id: DEV_RULE,
      matches: false,
      reasons: ["the rule is for environment 'dev', not 'prod'", "the rule is disabled"],
    },
    { rule_id: PROD_RULE, matches: true, reasons: [] },
  ],
};

/**
 * Chooses the environment and the repository, and pastes the claims.
 *
 * @param user - The user.
 * @param claims - What is pasted.
 */
async function fill(user: UserEvent, claims: string): Promise<void> {
  await user.click(await screen.findByRole("button", { name: /Environment/ }));
  await user.click(await screen.findByRole("option", { name: /prod/ }));
  await user.click(screen.getByRole("button", { name: /Repository/ }));
  await user.click(await screen.findByRole("option", { name: "analytics" }));
  await user.click(screen.getByRole("textbox", { name: "Claims" }));
  await user.paste(claims);
}

describe("CiTrustExplain", () => {
  it("explains the claims: the verdict, and why each rule does or does not match", async () => {
    const { server, user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        "POST /api/v1/ci-trust-rules/explain": { status: 200, body: REPORT },
      }),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "Explain a CI job's claims" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByRole("link", {
        name: "CI trust",
      }),
    ).toBeInTheDocument();
    await fill(user, JSON.stringify(CLAIMS));
    await user.click(screen.getByRole("button", { name: "Explain" }));

    const verdict = await screen.findByRole("region", { name: "Verdict" });
    expect(within(verdict).getByText("Accepted")).toBeInTheDocument();
    expect(verdict).toHaveTextContent(`accepted by rule ${PROD_RULE}`);
    expect(verdict).toHaveTextContent(
      "Issuer GitHub Actions, environment prod, repository analytics.",
    );
    const rules = screen.getByRole("region", { name: "Every rule" });
    const dev = within(rules).getByRole("link", { name: "analytics to dev" }).closest("li");
    expect(dev).toHaveTextContent("Does not match");
    expect(dev).toHaveTextContent("the rule is for environment 'dev', not 'prod'");
    expect(dev).toHaveTextContent("the rule is disabled");
    const prod = within(rules).getByRole("link", { name: "analytics to prod" }).closest("li");
    expect(prod).toHaveTextContent("Matches");
    const sent = server.sent.find((request) => request.method === "POST");
    expect(sent?.body).toEqual({ claims: CLAIMS, environment: "prod", repository: "analytics" });
    expect(server.sent.some((request) => request.path === "/api/v1/sign-in/session")).toBe(false);
  });

  it("names a refusal, the issuer when given, and a rule deleted since", async () => {
    const { server, user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        "POST /api/v1/ci-trust-rules/explain": {
          status: 200,
          body: {
            ...REPORT,
            verdict: "refused: no enabled rule for this issuer, environment and repository matches",
            matching: [],
            rules: [{ rule_id: "gone", matches: false, reasons: ["the rule is disabled"] }],
          },
        },
      }),
    });

    await fill(user, JSON.stringify({ ref: "refs/heads/main" }));
    await user.type(screen.getByRole("textbox", { name: "Issuer" }), "github");
    await user.click(screen.getByRole("button", { name: "Explain" }));

    const verdict = await screen.findByRole("region", { name: "Verdict" });
    expect(within(verdict).getByText("Refused")).toHaveClass("pill-bad");
    expect(screen.getByText("a rule deleted since")).toBeInTheDocument();
    expect(server.sent.find((request) => request.method === "POST")?.body).toEqual({
      claims: { ref: "refs/heads/main" },
      environment: "prod",
      repository: "analytics",
      issuer: "github",
    });
  });

  it("sends nothing for text that is no JSON object, or a token", async () => {
    const { server, user } = renderShell(PATH, { routes: ciTrustRoutes() });

    await fill(user, "ref: main");
    await user.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The claims are not JSON");

    await user.clear(screen.getByRole("textbox", { name: "Claims" }));
    await user.paste("[1, 2]");
    await user.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("must be one JSON object");

    await user.clear(screen.getByRole("textbox", { name: "Claims" }));
    await user.paste("eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJ4In0.c2lnbmF0dXJl");
    await user.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("looks like an ID token");

    expect(server.sent.filter((request) => request.method === "POST")).toEqual([]);
  });

  it("holds Explain until the form is complete, and keeps the server's refusal", async () => {
    const { user } = renderShell(PATH, {
      routes: ciTrustRoutes({
        "POST /api/v1/ci-trust-rules/explain": {
          status: 422,
          body: {
            type: "ValidationError",
            detail: "Give the issuer, or claims with an iss claim.",
          },
        },
      }),
    });

    const submit = await screen.findByRole("button", { name: "Explain" });
    expect(submit).toBeDisabled();
    expect(screen.getByText("Paste the claims.")).toBeInTheDocument();
    await fill(user, "{}");
    await user.click(submit);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Give the issuer, or claims with an iss claim.",
    );
    expect(screen.getByRole("textbox", { name: "Claims" })).toHaveValue("{}");
  });

  it("a non-administrator cannot use it", async () => {
    renderShell(PATH, {
      access: areasFor(false),
      me: { name: "kim", admin: false },
      routes: ciTrustRoutes(),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use CI trust" }),
    ).toBeInTheDocument();
  });
});
