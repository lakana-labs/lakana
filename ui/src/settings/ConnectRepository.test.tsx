import { act, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { areasFor } from "../shell/areas";
import { expectPending, held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const PAGE = "/settings/repositories/new";
const GITHUB_HTTPS = "https://github.com/acme/analytics";
const SSH_URL = "git@github.com:acme/analytics.git";
const ED25519 = {
  type: "ssh-ed25519",
  fingerprint: "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
  line: "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
};
const ECDSA = {
  type: "ecdsa-sha2-nistp256",
  fingerprint: "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
  line: "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTY=",
};
const RSA = {
  type: "ssh-rsa",
  fingerprint: "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
  line: "github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr",
};
const PROBE = {
  id: "p-1",
  kind: "host_keys",
  state: "done",
  outcome: "ok",
  message: "Found 3 host keys of github.com.",
  host_keys: [ED25519, ECDSA, RSA],
  created_at: "2026-11-11T08:00:00Z",
  finished_at: "2026-11-11T08:00:02Z",
};
const ACCESS_OK = {
  ...PROBE,
  id: "p-2",
  kind: "access",
  message: "ok: a worker read analytics with the deploy key of dev.",
  host_keys: null,
};
const DEV_KEY = {
  environment: "dev",
  public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDevKeyForAnalytics lakana analytics dev",
  fingerprint: "SHA256:dEvKeYfInGeRpRiNt0000000000000000000000000",
  made: true,
};
const PROD_KEY = {
  environment: "prod",
  public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIProdKeyForAnalytics lakana analytics prod",
  fingerprint: "SHA256:pRoDkEyFiNgErPrInT000000000000000000000000",
  made: false,
};
const DEPLOY_KEY = {
  secret: "analytics-deploy-key",
  keys: [DEV_KEY, PROD_KEY],
  skipped: ["staging"],
  replaced: [],
  notes: [],
};
const STEP_UP: Record<string, FakeRoute> = {
  "POST /api/v1/sign-in/step-up/options": {
    status: 200,
    body: { ceremony_id: "c-1", public_key: {} },
  },
  "POST /api/v1/sign-in/step-up": { status: 204 },
};

/**
 * The routes of the whole flow, answered at once; a test replaces the ones it is about.
 *
 * @param more - Routes that replace the defaults.
 * @returns The routes.
 */
function flow(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    "POST /api/v1/repository-probes": (request) => ({
      status: 202,
      body: (request.body as { kind: string }).kind === "access" ? ACCESS_OK : PROBE,
    }),
    "POST /api/v1/repositories": { status: 201, body: { name: "analytics" } },
    "POST /api/v1/repositories/analytics/deploy-key": { status: 201, body: DEPLOY_KEY },
    ...more,
  };
}

/** The page's controls. */
const ui = {
  url: () => screen.getByRole("textbox", { name: "Repository URL" }),
  name: () => screen.getByRole("textbox", { name: "Name in Lakana" }),
  fetch: () => screen.getByRole("button", { name: /^Fetch the host's keys/ }),
  create: () => screen.getByRole("button", { name: /^Create the repository and its keys/ }),
};

/**
 * Opens the page and enters a URL; the name follows on leaving the field.
 *
 * @param url - The URL.
 * @param routes - The routes.
 * @returns The rendered shell.
 */
async function enter(url: string, routes = flow()): Promise<ReturnType<typeof renderShell>> {
  const shell = renderShell(PAGE, { routes });
  await shell.user.type(await screen.findByRole("textbox", { name: "Repository URL" }), url);
  await shell.user.tab();
  return shell;
}

/**
 * Opens the page, enters a GitHub URL and fetches the host's keys.
 *
 * @param routes - The routes.
 * @returns The rendered shell.
 */
async function fetched(routes = flow()): Promise<ReturnType<typeof renderShell>> {
  const shell = await enter(GITHUB_HTTPS, routes);
  await shell.user.click(ui.fetch());
  await screen.findByText(ED25519.fingerprint);
  return shell;
}

/**
 * The POST requests sent, by path.
 *
 * @param shell - The rendered shell.
 * @returns Their paths.
 */
function posts(shell: ReturnType<typeof renderShell>): string[] {
  return shell.server.sent
    .filter((request) => request.method === "POST")
    .map((request) => request.path);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("ConnectRepository", () => {
  it("is reachable from Get started and has its breadcrumb", async () => {
    const { user } = renderShell("/", {
      routes: {
        "GET /api/v1/home/get-started": {
          status: 200,
          body: {
            audience: "admin",
            local: false,
            steps: [{ id: "connect_repository", done: false }],
            hidden: false,
            complete: false,
          },
        },
      },
    });

    await user.click(await screen.findByRole("link", { name: "Connect a repository" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Connect a repository" }),
    ).toBeInTheDocument();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "/settings",
    );
    expect(within(crumbs).getByText("Connect a repository")).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(document.title).toBe("Connect a repository · Lakana");
  });

  it("is not available to a person who is not an administrator", async () => {
    renderShell(PAGE, { access: areasFor(false, false), me: { admin: false } });

    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(
      "You cannot use Settings",
    );
    expect(screen.queryByRole("textbox", { name: "Repository URL" })).not.toBeInTheDocument();
  });

  it("turns a GitHub HTTPS URL into its SSH form and proposes the name", async () => {
    await enter(GITHUB_HTTPS);

    expect(ui.name()).toHaveValue("analytics");
    expect(screen.getByText(/A GitHub repository/)).toHaveTextContent(SSH_URL);
    expect(ui.fetch()).toBeEnabled();
  });

  it("keeps a name the person typed", async () => {
    const { user } = renderShell(PAGE, { routes: flow() });
    await user.type(await screen.findByRole("textbox", { name: "Name in Lakana" }), "warehouse");
    await user.type(ui.url(), SSH_URL);
    await user.tab();

    expect(ui.name()).toHaveValue("warehouse");
  });

  it("names what is wrong with the URL or the name when the field is left", async () => {
    const { user } = renderShell(PAGE, { routes: flow() });
    await user.click(await screen.findByRole("textbox", { name: "Repository URL" }));
    await user.tab();
    expect(screen.getByText(/Enter the repository's URL/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fetch the host's keys" })).not.toBeInTheDocument();

    await user.type(ui.url(), "analytics");
    await user.tab();
    expect(screen.getByText(/This is not a repository URL/)).toBeInTheDocument();
    expect(ui.url()).toHaveAttribute("aria-invalid", "true");

    await user.clear(ui.url());
    await user.type(ui.url(), "git@git.example.com:team/my.analytics.git");
    await user.tab();
    expect(screen.queryByText(/This is not a repository URL/)).not.toBeInTheDocument();
    expect(ui.name()).toHaveValue("my-analytics");
    expect(screen.getByText(/Lakana connects to it over SSH/)).toBeInTheDocument();
    expect(ui.fetch()).toBeEnabled();
    await user.type(ui.name(), ".v2");
    await user.tab();
    expect(screen.getByText(/no dots, slashes, percent signs or spaces/)).toHaveTextContent(
      "Try my-analytics-v2.",
    );
    expect(ui.fetch()).toBeDisabled();

    await user.clear(ui.url());
    await user.type(ui.url(), "git@github.com?@evil.example:acme/analytics.git");
    await user.tab();
    expect(screen.getByText(/A repository URL has no/)).toBeInTheDocument();
    expect(screen.queryByText(/A GitHub repository/)).not.toBeInTheDocument();

    await user.clear(ui.name());
    await user.tab();
    expect(screen.getByText("Enter a name for the repository.")).toBeInTheDocument();
  });

  it("quotes the URL in the CLI line", async () => {
    await enter("https://git.example.com/team/it's.git");

    expect(screen.getByText(/Lakana cannot tell this host's SSH address/)).toBeInTheDocument();
    expect(
      screen.getByText(
        "lakana repo add 'it'\\''s' --kind https --url 'https://git.example.com/team/it'\\''s.git' --credential-secret 'it'\\''s'",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/A token belongs to a person and expires/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fetch the host's keys" })).not.toBeInTheDocument();
  });

  it("offers a token for every host, as the second way", async () => {
    const { user } = await enter(GITHUB_HTTPS);
    const toggle = screen.getByRole("button", { name: "Use a token instead" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByText(
        "lakana repo add 'analytics' --kind https --url 'https://github.com/acme/analytics' --credential-secret 'analytics'",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /How to store the token/ })).toHaveAttribute(
      "href",
      "https://lakana-labs.github.io/lakana/how-to/projects/#register-the-repository-admin",
    );
  });

  it("shows each host key on its own row, ed25519 first and the others on request", async () => {
    const { user } = await fetched();

    expect(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" })).not.toBeChecked();
    expect(screen.queryByText(RSA.fingerprint)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "GitHub's published fingerprints" })).toHaveAttribute(
      "href",
      "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints",
    );

    await user.click(screen.getByRole("button", { name: "Show other key types (2)" }));

    expect(screen.getByText(RSA.fingerprint)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "This matches: ssh-rsa" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show other key types/ })).not.toBeInTheDocument();
  });

  it("offers every key at once when the host has no ed25519 key", async () => {
    const { user } = await enter(
      GITHUB_HTTPS,
      flow({
        "POST /api/v1/repository-probes": {
          status: 202,
          body: { ...PROBE, host_keys: [ECDSA, RSA] },
        },
      }),
    );
    await user.click(ui.fetch());

    expect(
      await screen.findByRole("checkbox", { name: "This matches: ecdsa-sha2-nistp256" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "This matches: ssh-rsa" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show other key types/ })).not.toBeInTheDocument();
  });

  it("says so when the host offered no key", async () => {
    const { user } = await enter(
      "git@git.example.com:team/analytics.git",
      flow({
        "POST /api/v1/repository-probes": { status: 202, body: { ...PROBE, host_keys: null } },
      }),
    );
    await user.click(ui.fetch());

    expect(await screen.findByText(/The host offered no SSH key/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /published fingerprints/ })).toBeNull();
  });

  it("announces the keys found and keeps focus on the fetch", async () => {
    await fetched();

    expect(
      screen.getByText(
        "Found 3 keys of the host. Compare each fingerprint, then tick the ones that match.",
      ),
    ).toHaveAttribute("role", "status");
    expect(ui.fetch()).toHaveFocus();
  });

  it("stores nothing before the fingerprints are confirmed", async () => {
    const shell = await fetched();

    expect(ui.create()).toBeDisabled();
    expect(screen.getByText("Tick each key that matches, above, first.")).toBeInTheDocument();
    expect(posts(shell)).toEqual(["/api/v1/repository-probes"]);
  });

  it("sends exactly the confirmed known_hosts lines", async () => {
    const shell = await fetched();
    await shell.user.click(screen.getByRole("button", { name: "Show other key types (2)" }));
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-rsa" }));
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-rsa" }));
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-rsa" }));

    await shell.user.click(ui.create());

    await screen.findByText(DEV_KEY.public_key);
    const created = shell.server.sent.find((request) => request.path === "/api/v1/repositories");
    expect(created?.body).toEqual({
      name: "analytics",
      kind: "ssh",
      url: SSH_URL,
      ssh_known_hosts: `${ED25519.line}\n${RSA.line}\n`,
    });
  });

  it("sends only the keys ticked one by one", async () => {
    const shell = await fetched();
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));

    await shell.user.click(ui.create());

    await screen.findByText(DEV_KEY.public_key);
    const created = shell.server.sent.find((request) => request.path === "/api/v1/repositories");
    expect((created?.body as { ssh_known_hosts: string }).ssh_known_hosts).toBe(
      `${ED25519.line}\n`,
    );
  });

  it("asks for one passkey touch for creating and the key", async () => {
    const webauthn = fakeWebAuthn();
    let creates = 0;
    const shell = await fetched(
      flow({
        ...STEP_UP,
        "POST /api/v1/repositories": () => {
          creates += 1;
          return creates === 1
            ? { status: 403, body: { type: "StepUpRequired", detail: "Touch your passkey." } }
            : { status: 201, body: { name: "analytics" } };
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));

    await shell.user.click(ui.create());

    expect(await screen.findByText(DEV_KEY.public_key)).toBeInTheDocument();
    expect(webauthn.get).toHaveBeenCalledTimes(1);
    expect(posts(shell)).toEqual([
      "/api/v1/repository-probes",
      "/api/v1/repositories",
      "/api/v1/sign-in/step-up/options",
      "/api/v1/sign-in/step-up",
      "/api/v1/repositories",
      "/api/v1/repositories/analytics/deploy-key",
    ]);
  });

  it("shows the public key, the deploy-key page and the environments", async () => {
    const shell = await fetched();
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    const dev = (await screen.findByText(DEV_KEY.public_key)).closest("li") as HTMLElement;
    expect(within(dev).getByText("dev")).toBeInTheDocument();
    expect(within(dev).getByText(DEV_KEY.fingerprint)).toBeInTheDocument();
    expect(within(dev).getByRole("button", { name: "Copy key" })).toBeVisible();
    const prod = screen.getByText(PROD_KEY.public_key).closest("li") as HTMLElement;
    expect(within(prod).getByText("made earlier")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open GitHub's deploy-key page" })).toHaveAttribute(
      "href",
      "https://github.com/acme/analytics/settings/keys/new",
    );
    expect(screen.getByText(/Keep it read-only/)).toBeInTheDocument();
    expect(screen.getByText(/No key in staging/)).toHaveTextContent(
      "lakana repo deploy-key analytics",
    );
    expect(screen.getByText(/An environment created later has no key yet/)).toBeInTheDocument();
    expect(ui.url()).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Create the repository and its keys" })).toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "4. Test access" })).toHaveFocus();
    expect(screen.getByText(/^Made keys for dev, prod\./)).toHaveAttribute("role", "status");
  });

  it("retries only the key after a failed key call", async () => {
    let tries = 0;
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories/analytics/deploy-key": () => {
          tries += 1;
          return tries === 1
            ? {
                status: 409,
                body: {
                  type: "ConflictError",
                  detail: "A secret analytics-deploy-key in dev is not a key made for analytics.",
                },
              }
            : { status: 201, body: DEPLOY_KEY };
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The repository is registered; its keys are not made yet.");
    expect(alert).toHaveTextContent("is not a key made for analytics");
    await shell.user.click(screen.getByRole("button", { name: "Make the keys" }));

    expect(await screen.findByText(DEV_KEY.public_key)).toBeInTheDocument();
    expect(posts(shell)).toEqual([
      "/api/v1/repository-probes",
      "/api/v1/repositories",
      "/api/v1/repositories/analytics/deploy-key",
      "/api/v1/repositories/analytics/deploy-key",
    ]);
    expect(screen.getByRole("heading", { level: 2, name: "4. Test access" })).toHaveFocus();
  });

  it("keeps the step when registering is refused", async () => {
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories": {
          status: 422,
          body: { type: "ValidationError", detail: "Repository name must not contain '$'." },
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    expect(await screen.findByRole("alert")).toHaveTextContent("must not contain");
    expect(ui.create()).toBeEnabled();
    expect(ui.name()).toBeEnabled();
    expect(posts(shell)).not.toContain("/api/v1/repositories/analytics/deploy-key");
  });

  it("goes on to the keys when the same repository is already registered", async () => {
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories": {
          status: 409,
          body: {
            type: "AlreadyExistsError",
            detail: "A repository named 'analytics' already exists.",
          },
        },
        "GET /api/v1/repositories/analytics": {
          status: 200,
          body: {
            name: "analytics",
            kind: "ssh",
            url: SSH_URL,
            // The same line, written down differently: spaces and blank lines do not count.
            ssh_known_hosts: `\n  ${ED25519.line.replaceAll(" ", "  ")}  \n\n`,
            deploy_keys: [],
          },
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    expect(await screen.findByText(DEV_KEY.public_key)).toBeInTheDocument();
    expect(screen.getByText(/analytics was already registered with this URL/)).toBeInTheDocument();
    expect(posts(shell)).toEqual([
      "/api/v1/repository-probes",
      "/api/v1/repositories",
      "/api/v1/repositories/analytics/deploy-key",
    ]);
  });

  it.each([
    ["other host keys", `${ED25519.line}\n${RSA.line}\n`],
    ["no host keys", null],
  ])("stays when the same address is registered with %s", async (_, stored) => {
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories": {
          status: 409,
          body: {
            type: "AlreadyExistsError",
            detail: "A repository named 'analytics' already exists.",
          },
        },
        "GET /api/v1/repositories/analytics": {
          status: 200,
          body: {
            name: "analytics",
            kind: "ssh",
            url: SSH_URL,
            ssh_known_hosts: stored,
            deploy_keys: [],
          },
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "A repository with this address is already registered with other host keys.",
    );
    expect(alert).toHaveTextContent("lakana repo show 'analytics'");
    expect(alert).toHaveTextContent(
      "lakana repo update 'analytics' --known-hosts-file known_hosts",
    );
    expect(alert).toHaveTextContent(
      "lakana repo host-keys 'git@github.com:acme/analytics.git' --save known_hosts",
    );
    expect(alert).not.toHaveTextContent("write them to a file");
    expect(screen.queryByText(/its keys are not made yet/)).not.toBeInTheDocument();
    expect(ui.create()).toBeEnabled();
    expect(posts(shell)).not.toContain("/api/v1/repositories/analytics/deploy-key");
  });

  const TAKEN = {
    status: 409,
    body: { type: "AlreadyExistsError", detail: "A repository named 'analytics' already exists." },
  };
  it.each<[string, FakeRoute, FakeRoute, string[]]>([
    [
      "the name is taken by another address",
      TAKEN,
      {
        status: 200,
        body: {
          name: "analytics",
          kind: "ssh",
          url: "git@github.com:acme/other.git",
          ssh_known_hosts: null,
        },
      },
      [
        "already exists",
        "A repository named analytics already exists with another URL. Choose another name above.",
      ],
    ],
    [
      "the same address is registered as another kind",
      TAKEN,
      {
        status: 200,
        body: { name: "analytics", kind: "https", url: SSH_URL, ssh_known_hosts: null },
      },
      [
        "A https repository with this address is already registered as analytics.",
        "lakana repo show 'analytics'",
        "or choose another name above.",
      ],
    ],
    [
      "the repository cannot be read to compare",
      TAKEN,
      { status: 500, body: { type: "HttpError", detail: "Internal error." } },
      ["already exists", "Lakana could not read it to compare.", "lakana repo show 'analytics'"],
    ],
    [
      "the write failed for another reason",
      {
        status: 409,
        body: { type: "ConflictError", detail: "The repository could not be saved." },
      },
      { status: 500, body: null },
      [
        "The repository could not be saved.",
        "Try again; if it happens again, the server's log has the details.",
      ],
    ],
  ])("names the cause and next step when %s", async (_, created, existing, sentences) => {
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories": created,
        "GET /api/v1/repositories/analytics": existing,
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    const alert = await screen.findByRole("alert");
    for (const sentence of sentences) {
      expect(alert).toHaveTextContent(sentence);
    }
    expect(alert).not.toHaveTextContent("its keys are not made yet");
    expect(ui.name()).toBeEnabled();
    expect(posts(shell)).not.toContain("/api/v1/repositories/analytics/deploy-key");
  });

  it("keeps focus on Create while it runs and after it fails, and creates once", async () => {
    let answer: (value: { status: number; body: unknown }) => void = () => undefined;
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories": () =>
          new Promise((resolve) => {
            answer = resolve;
          }),
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());
    expectPending(ui.create());
    await shell.user.click(ui.create());
    expect(ui.create()).toHaveFocus();

    await act(async () => {
      answer({ status: 422, body: { type: "ValidationError", detail: "Not a valid name." } });
      await Promise.resolve();
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Not a valid name.");
    expect(ui.create()).toHaveFocus();
    expect(posts(shell).filter((path) => path === "/api/v1/repositories")).toHaveLength(1);
  });

  it("shows the server's notes on the keys", async () => {
    const note = "The old key for dev stops working: replace it at your git host now.";
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories/analytics/deploy-key": {
          status: 201,
          body: { ...DEPLOY_KEY, replaced: ["dev"], notes: [note] },
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    expect(await screen.findByText(note)).toBeInTheDocument();
  });

  it("says who makes the key when no environment got one", async () => {
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories/analytics/deploy-key": {
          status: 201,
          body: { ...DEPLOY_KEY, keys: [], skipped: ["dev", "prod"] },
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    expect(await screen.findByText(/No key in dev, prod/)).toBeInTheDocument();
    expect(screen.getByText(/No environment got a key/)).toHaveAttribute("role", "alert");
    expect(
      screen.getByRole("heading", { level: 2, name: "3. Create the repository and its keys" }),
    ).toHaveFocus();
    expect(screen.queryByRole("button", { name: /Test access/ })).not.toBeInTheDocument();
  });

  it("says that no worker answered", async () => {
    const { user } = await enter(
      GITHUB_HTTPS,
      flow({
        "POST /api/v1/repository-probes": {
          status: 202,
          body: { ...PROBE, outcome: "no_worker", message: "No worker took it.", host_keys: null },
        },
      }),
    );
    await user.click(ui.fetch());

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No worker answered within two minutes. Start one (docker compose up -d worker, or lakana server start --local), then try again.",
    );
    expect(ui.fetch()).toBeEnabled();
  });

  it("names the server's reason when a check cannot be queued", async () => {
    const { user } = await enter(
      GITHUB_HTTPS,
      flow({
        "POST /api/v1/repository-probes": {
          status: 503,
          body: { type: "ServiceUnavailable", detail: "No worker is running. Start one." },
        },
      }),
    );
    await user.click(ui.fetch());

    expect(await screen.findByRole("alert")).toHaveTextContent("No worker is running. Start one.");
  });

  it("names the cause the host-key check found", async () => {
    const { user } = await enter(
      GITHUB_HTTPS,
      flow({
        "POST /api/v1/repository-probes": {
          status: 202,
          body: {
            ...PROBE,
            outcome: "unreachable",
            message: "The worker could not reach github.com:22. Check the host and port.",
            host_keys: null,
          },
        },
      }),
    );
    await user.click(ui.fetch());

    expect(await screen.findByRole("alert")).toHaveTextContent("could not reach github.com:22");
    expect(screen.queryByRole("button", { name: /Create the repository/ })).toBeNull();
  });

  it("says something went wrong when a check ends without a message", async () => {
    const { user } = await enter(
      GITHUB_HTTPS,
      flow({
        "POST /api/v1/repository-probes": {
          status: 202,
          body: { ...PROBE, outcome: "failed", message: null, host_keys: null },
        },
      }),
    );
    await user.click(ui.fetch());

    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
  });

  it("says what is happening after a second of waiting", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let polls = 0;
    const { user, server } = await enter(
      GITHUB_HTTPS,
      flow({
        "POST /api/v1/repository-probes": { status: 202, body: { ...PROBE, state: "queued" } },
        "GET /api/v1/repository-probes/p-1": () => {
          polls += 1;
          return { status: 200, body: polls < 3 ? { ...PROBE, state: "running" } : PROBE };
        },
      }),
    );
    await user.click(ui.fetch());
    expect(screen.queryByText("Asking a worker to fetch the keys…")).not.toBeInTheDocument();
    // The sign shows at once, a second before the words.
    expectPending(ui.fetch());
    // The button keeps focus while the worker runs the check; a second press asks nothing more.
    await user.click(ui.fetch());
    expect(ui.fetch()).toHaveFocus();
    expect(server.sent.filter((request) => request.method === "POST")).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("Asking a worker to fetch the keys…")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(await screen.findByText(ED25519.fingerprint)).toBeInTheDocument();
    expect(screen.queryByText("Asking a worker to fetch the keys…")).not.toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(
      server.sent.filter((request) => request.method === "GET" && request.path.includes("probes")),
    ).toHaveLength(3);
  });

  it("stops waiting for a check when the page is left", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { user, server, router } = await enter(
      GITHUB_HTTPS,
      flow({
        "POST /api/v1/repository-probes": { status: 202, body: { ...PROBE, state: "queued" } },
        "GET /api/v1/repository-probes/p-1": { status: 200, body: { ...PROBE, state: "running" } },
      }),
    );
    await user.click(ui.fetch());
    await act(async () => {
      await router.navigate("/settings/status");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(
      server.sent.filter((request) => request.path === "/api/v1/repository-probes/p-1"),
    ).toHaveLength(0);
  });

  it("forgets the fetched keys when the URL changes", async () => {
    const { user } = await fetched();
    await user.type(ui.url(), "-old");
    await user.tab();

    expect(screen.queryByText(ED25519.fingerprint)).not.toBeInTheDocument();
  });

  it("tests access in the first environment written and suggests what comes next", async () => {
    const shell = await fetched();
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());

    await shell.user.click(await screen.findByRole("button", { name: "Test access in dev" }));

    expect(await screen.findByText(ACCESS_OK.message)).toHaveAttribute("role", "status");
    expect(screen.getByRole("button", { name: "Test access in dev" })).toHaveFocus();
    expect(shell.server.sent.at(-1)?.body).toEqual({
      kind: "access",
      repository: "analytics",
      environment: "dev",
    });
    expect(screen.getByText("lakana deploy 'analytics' --env 'dev'")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Deploy from GitHub Actions" })).toHaveAttribute(
      "href",
      "https://lakana-labs.github.io/lakana/how-to/deploy-from-github-actions/",
    );
  });

  it.each([
    ["git@gitlab.com:acme/analytics.git", "Open GitLab's deploy-key page", "Deploy from GitLab CI"],
    ["git@git.example.com:team/analytics.git", null, null],
  ])("links the deploy-key page and CI of the host of %s", async (url, page, ci) => {
    const { user } = await enter(url);
    await user.click(ui.fetch());
    await user.click(await screen.findByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await user.click(ui.create());
    await user.click(await screen.findByRole("button", { name: "Test access in dev" }));
    await screen.findByText(ACCESS_OK.message);

    if (page === null) {
      expect(screen.getByText(/open the repository's settings and its deploy keys/)).toBeVisible();
      expect(screen.queryByRole("link", { name: /deploy-key page|Deploy from/ })).toBeNull();
    } else {
      expect(screen.getByRole("link", { name: page })).toBeVisible();
      expect(screen.getByRole("link", { name: ci })).toHaveAttribute(
        "href",
        "https://lakana-labs.github.io/lakana/how-to/deploy-from-gitlab-ci/",
      );
    }
  });

  it("shows the pending sign on Test access at once and keeps focus there", async () => {
    const request = held();
    const shell = await fetched(
      flow({
        "POST /api/v1/repository-probes": (sent) =>
          (sent.body as { kind: string }).kind === "access"
            ? request.route()
            : { status: 202, body: PROBE },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());
    const test = await screen.findByRole("button", { name: "Test access in dev" });

    await shell.user.click(test);

    expectPending(test);
    expect(test).toHaveFocus();
    request.release({ status: 202, body: ACCESS_OK });
    expect(await screen.findByText(ACCESS_OK.message)).toBeInTheDocument();
  });

  it("asks for the keys once when the button is pressed twice in the same instant", async () => {
    const shell = await enter(
      GITHUB_HTTPS,
      flow({ "POST /api/v1/repository-probes": held().route }),
    );

    act(() => {
      ui.fetch().click();
      ui.fetch().click();
    });

    expectPending(ui.fetch());
    expect(posts(shell)).toEqual(["/api/v1/repository-probes"]);
  });

  it("names the cause and next step of a failed access test", async () => {
    const refused = {
      ...ACCESS_OK,
      outcome: "auth_refused",
      message: "The git host refused the key of dev. Add it as a deploy key, then test again.",
    };
    const shell = await fetched(
      flow({
        "POST /api/v1/repository-probes": (request) => ({
          status: 202,
          body: (request.body as { kind: string }).kind === "access" ? refused : PROBE,
        }),
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());
    await shell.user.click(await screen.findByRole("button", { name: "Test access in dev" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(refused.message);
    expect(screen.queryByText(/lakana deploy/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test access in dev" })).toBeEnabled();
  });

  it("never holds a private key", async () => {
    const pem =
      "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----";
    const shell = await fetched(
      flow({
        "POST /api/v1/repositories/analytics/deploy-key": {
          status: 201,
          body: { ...DEPLOY_KEY, keys: [{ ...DEV_KEY, private_key: pem }], private_key: pem },
        },
      }),
    );
    await shell.user.click(screen.getByRole("checkbox", { name: "This matches: ssh-ed25519" }));
    await shell.user.click(ui.create());
    await shell.user.click(await screen.findByRole("button", { name: "Test access in dev" }));
    await screen.findByText(ACCESS_OK.message);

    expect(document.body.textContent).not.toContain("PRIVATE KEY");
    for (const request of shell.server.sent) {
      expect(JSON.stringify(request.body ?? null)).not.toContain("PRIVATE KEY");
    }
  });
});
