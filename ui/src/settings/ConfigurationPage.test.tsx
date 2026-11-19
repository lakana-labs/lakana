import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { areasFor } from "../shell/areas";
import { aSetting, CONFIGURATION_FULL, CONFIGURATION_SAMPLE } from "../test/configuration";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";

const PATH = "/settings/configuration";
const ROUTE = "GET /api/v1/settings/configuration";

/**
 * The configuration route answering the given settings.
 *
 * @param settings - The settings; the sample by default.
 * @returns The routes.
 */
function answering(settings: readonly unknown[] = CONFIGURATION_SAMPLE): Record<string, FakeRoute> {
  return { [ROUTE]: { status: 200, body: settings } };
}

/**
 * The region of the list, once it is read.
 *
 * @returns The region.
 */
async function serverSettings(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Server settings" });
}

/**
 * The names of the data rows in the list, in order.
 *
 * @param region - The list's region.
 * @returns The names.
 */
function rowNames(region: HTMLElement): string[] {
  return [...region.querySelectorAll("td .row-name")].map((name) => name.textContent);
}

/**
 * The group rows' names, in order.
 *
 * @param region - The list's region.
 * @returns The names.
 */
function groupNames(region: HTMLElement): string[] {
  return [...region.querySelectorAll('th[scope="rowgroup"]')].map((head) => head.textContent);
}

/**
 * The data row of a setting.
 *
 * @param region - The list's region.
 * @param name - The setting's name.
 * @returns The row.
 */
function rowOf(region: HTMLElement, name: string): HTMLElement {
  return within(region).getByText(name).closest("tr") as HTMLElement;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ConfigurationPage", () => {
  it("renders all 89 settings in groups", async () => {
    renderShell(PATH, { routes: answering(CONFIGURATION_FULL) });

    const region = await serverSettings();
    const names = rowNames(region);
    expect(names).toHaveLength(89);
    expect(names[0]).toBe("LAKANA_DATABASE__URL");
    expect(names.at(-1)).toBe("LAKANA_UI__PASSKEY_OPTIONS_PER_NETWORK_PER_MINUTE");
    expect(names.slice(0, 11)).toEqual([
      "LAKANA_DATABASE__URL",
      "LAKANA_DATABASE__ECHO",
      "LAKANA_BOOTSTRAP_ADMIN",
      "LAKANA_BOOTSTRAP_PAT_KEY_ID",
      "LAKANA_BOOTSTRAP_PAT_SECRET",
      "LAKANA_BOOTSTRAP_TOKEN_FILE",
      "LAKANA_HOST",
      "LAKANA_PORT",
      "LAKANA_TRUSTED_PROXIES",
      "LAKANA_REPO_ROOTS",
      "LAKANA_MAIN_LANGUAGE",
    ]);
    expect(groupNames(region)).toEqual([
      "LAKANA_DATABASE__*",
      "General",
      "LAKANA_SECRETS__*",
      "LAKANA_TASK_TOKENS__*",
      "LAKANA_ARTIFACTS__*",
      "LAKANA_TABLES__*",
      "LAKANA_TABLE_READS__*",
      "LAKANA_HEALTH__*",
      "LAKANA_APPEND__*",
      "LAKANA_REQUEST_BODY__*",
      "LAKANA_AUTH_AUDIT__*",
      "LAKANA_DENIAL_AUDIT__*",
      "LAKANA_VIEW_AUDIT__*",
      "LAKANA_AUDIT__*",
      "LAKANA_QUERY_LIMITS__*",
      "LAKANA_RUN__*",
      "LAKANA_LOG__*",
      "LAKANA_CI_OIDC__*",
      "LAKANA_READINESS__*",
      "LAKANA_UI__*",
    ]);
    const general = within(region).getByRole("rowheader", { name: "General" });
    expect(general).toHaveAttribute("colspan", "3");
    expect(
      within(region)
        .getAllByRole("columnheader")
        .map((head) => head.textContent),
    ).toEqual(["Setting", "Value", "Source"]);
    expect(region.querySelectorAll("tbody")).toHaveLength(20);
  });

  it("says where values change", async () => {
    renderShell(PATH, { routes: answering() });

    expect(
      await screen.findByText(
        "Values change in the server's .env file (or its environment) and take effect after a restart.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The server's settings as it runs now. A secret shows only whether it is set.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Workers and the launcher read their own settings; they are not shown here.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Every setting is described in the settings reference" }),
    ).toHaveAttribute("href", "https://lakana-labs.github.io/lakana/reference/settings/");
    expect(screen.getByRole("heading", { level: 1, name: "Configuration" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Configuration" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.queryByRole("button", { name: /^(Save|Edit)/ })).not.toBeInTheDocument();
  });

  it("never shows a secret's value, even if the server sent one", async () => {
    const calls = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method),
    );
    const { storage } = renderShell(PATH, {
      routes: answering(
        CONFIGURATION_SAMPLE.map((setting) =>
          setting.name === "LAKANA_SECRETS__MASTER_KEY"
            ? { ...setting, value: "mk-sentinel" }
            : setting,
        ),
      ),
    });

    const region = await serverSettings();
    expect(rowOf(region, "LAKANA_SECRETS__MASTER_KEY")).toHaveTextContent(
      "LAKANA_SECRETS__MASTER_KEYsecret, setset",
    );
    expect(rowOf(region, "LAKANA_RUN__HTTPS_PROXY")).toHaveTextContent("secret, not set");
    expect(document.title).not.toContain("mk-sentinel");
    expect(document.head.outerHTML).not.toContain("mk-sentinel");
    expect(document.body.outerHTML).not.toContain("mk-sentinel");
    for (const element of document.querySelectorAll("*")) {
      for (const attribute of element.attributes) {
        expect(attribute.value).not.toContain("mk-sentinel");
      }
    }
    expect(JSON.stringify(storage)).not.toContain("mk-sentinel");
    for (const kept of [localStorage, sessionStorage]) {
      for (let index = 0; index < kept.length; index += 1) {
        const key = kept.key(index) ?? "";
        expect(`${key}=${kept.getItem(key) ?? ""}`).not.toContain("mk-sentinel");
      }
    }
    for (const spy of calls) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain("mk-sentinel");
    }
  });

  it("shows the source in words", async () => {
    renderShell(PATH, { routes: answering() });

    const region = await serverSettings();
    expect(within(rowOf(region, "LAKANA_PORT")).getAllByRole("cell").at(-1)).toHaveTextContent(
      /^set$/,
    );
    expect(within(rowOf(region, "LAKANA_HOST")).getAllByRole("cell").at(-1)).toHaveTextContent(
      /^default$/,
    );
    expect(rowOf(region, "LAKANA_PORT")).toHaveTextContent("8000");
    expect(rowOf(region, "LAKANA_RUN__VENV_MAX_BYTES")).toHaveTextContent("10737418240");
    expect(rowOf(region, "LAKANA_DATABASE__ECHO")).toHaveTextContent("false");
    expect(rowOf(region, "LAKANA_CI_OIDC__ALGORITHMS")).toHaveTextContent('["RS256"]');
    expect(rowOf(region, "LAKANA_RUN__INGESTION_ROOTS")).toHaveTextContent('{"raw":"/srv/raw"}');
    const unset = within(rowOf(region, "LAKANA_RUN__CA_BUNDLE")).getByText("not set");
    expect(unset).toHaveClass("setting-unset");
  });

  it("finds a setting by any part of its name", async () => {
    const { user, router } = renderShell(PATH, { routes: answering() });
    const region = await serverSettings();

    await user.type(screen.getByRole("searchbox", { name: "Find a setting" }), "lease");

    await waitFor(() => {
      expect(router.state.location.search).toBe("?q=lease");
    });
    expect(router.state.historyAction).toBe("REPLACE");
    expect(rowNames(region)).toEqual(["LAKANA_RUN__LEASE_SECONDS"]);
    expect(groupNames(region)).toEqual(["LAKANA_RUN__*"]);

    await user.clear(screen.getByRole("searchbox", { name: "Find a setting" }));
    await user.type(screen.getByRole("searchbox", { name: "Find a setting" }), "LAKANA_run");
    expect(rowNames(region)).toEqual([
      "LAKANA_RUN__LEASE_SECONDS",
      "LAKANA_RUN__VENV_MAX_BYTES",
      "LAKANA_RUN__HTTPS_PROXY",
      "LAKANA_RUN__CA_BUNDLE",
      "LAKANA_RUN__INGESTION_ROOTS",
    ]);
    await user.clear(screen.getByRole("searchbox", { name: "Find a setting" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    expect(rowNames(region)).toHaveLength(CONFIGURATION_SAMPLE.length);
  });

  it("says when nothing matches and keeps the focus", async () => {
    const { user } = renderShell(PATH, { routes: answering() });
    await serverSettings();
    const field = screen.getByRole("searchbox", { name: "Find a setting" });

    await user.type(field, "zzz");

    expect(screen.getByText("No setting matches “zzz”.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Server settings" })).not.toBeInTheDocument();
    expect(field).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Clear search" }));

    const region = await serverSettings();
    expect(rowNames(region)).toHaveLength(CONFIGURATION_SAMPLE.length);
    expect(screen.getByRole("searchbox", { name: "Find a setting" })).toHaveValue("");
    expect(screen.getByRole("searchbox", { name: "Find a setting" })).toHaveFocus();
  });

  it("opens filtered from the address", async () => {
    renderShell(`${PATH}?q=proxy`, { routes: answering(CONFIGURATION_FULL) });

    const region = await serverSettings();
    expect(rowNames(region)).toEqual([
      "LAKANA_RUN__HTTPS_PROXY",
      "LAKANA_RUN__NO_PROXY",
      "LAKANA_CI_OIDC__PROXY",
    ]);
    expect(screen.getByRole("searchbox", { name: "Find a setting" })).toHaveValue("proxy");
  });

  it("keeps names whole and wraps values", async () => {
    const bundle = `/etc/ssl/${"certs/".repeat(48)}ca.pem`;
    renderShell(PATH, {
      routes: answering([aSetting("LAKANA_RUN__CA_BUNDLE", bundle, { source: "set" })]),
    });

    const region = await serverSettings();
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveClass("list-scroll", "configuration-scroll");
    expect(within(region).getByRole("table")).toHaveClass("list", "list-static");
    const name = within(region).getByText("LAKANA_RUN__CA_BUNDLE");
    expect(name).toHaveClass("row-name", "setting-name");
    expect(name.tagName).toBe("SPAN");
    expect(within(region).queryByRole("link")).not.toBeInTheDocument();
    const value = within(region).getByText(bundle).closest("td");
    expect(bundle.length).toBeGreaterThanOrEqual(300);
    expect(value).toHaveClass("setting-value");
  });

  it("marks a hidden character in a value", async () => {
    renderShell(PATH, {
      routes: answering([aSetting("LAKANA_BOOTSTRAP_ADMIN", "adm‮in", { source: "set" })]),
    });

    const region = await serverSettings();
    expect(rowOf(region, "LAKANA_BOOTSTRAP_ADMIN")).toHaveTextContent("admU+202Ein");
    expect(region.textContent).not.toMatch(/‮/u);
  });

  it("a failed read keeps the focus", async () => {
    let calls = 0;
    const { user } = renderShell(PATH, {
      routes: {
        [ROUTE]: () => {
          calls += 1;
          return calls === 1
            ? { status: 500, body: { detail: "The server could not answer." } }
            : { status: 200, body: CONFIGURATION_SAMPLE };
        },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The server could not answer.");
    await user.click(screen.getByRole("button", { name: "Try again" }));

    await serverSettings();
    expect(screen.getByRole("heading", { level: 1, name: "Configuration" })).toHaveFocus();
  });

  it("a 403 shows the server's sentence", async () => {
    let calls = 0;
    const { user } = renderShell(PATH, {
      routes: {
        [ROUTE]: () => {
          calls += 1;
          return calls === 1
            ? { status: 500, body: { detail: "The server could not answer." } }
            : {
                status: 403,
                body: {
                  type: "PermissionDenied",
                  detail: "Reading the server's configuration needs the ADMIN role.",
                },
              };
        },
      },
    });
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Reading the server's configuration needs the ADMIN role.",
      );
    });
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Configuration" })).toHaveFocus();
  });

  it("a person who is not an administrator cannot use Configuration", async () => {
    const { server } = renderShell(PATH, {
      access: areasFor(false),
      me: { name: "kim", admin: false },
      routes: answering(),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Configuration" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Configuration" })).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.path.startsWith("/api/v1/settings/"))).toBe(false);
  });
});
