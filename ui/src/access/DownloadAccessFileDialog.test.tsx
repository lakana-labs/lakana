import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ManagedScopeOut } from "../api/accessFile";
import { saveBlob } from "../shell/saveFile";
import { anEnvironment } from "../test/access";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { exportName } from "./DownloadAccessFileDialog";

vi.mock("../shell/saveFile", async (original) => ({
  ...(await original<typeof import("../shell/saveFile")>()),
  saveBlob: vi.fn(),
}));

const PATH = "/settings/access/access-file";
const UNWRITABLE =
  "environment 'a b' cannot be exported: its name cannot be written in an access file.";

/**
 * A managed scope.
 *
 * @param scope - `instance` or an environment's name.
 * @param file - The file that manages it.
 * @returns The scope.
 */
function managed(scope: string, file = "access.yaml"): ManagedScopeOut {
  return {
    scope,
    file_name: file,
    file_sha256: "ab12",
    source_commit: null,
    applied_by: "admin",
    applied_at: "2026-11-17T09:00:00Z",
  };
}

/**
 * Opens Download access file.
 *
 * @param scopes - The managed scopes.
 * @param more - Routes that differ.
 * @returns The dialog and the rendered shell.
 */
async function openDialog(
  scopes: readonly ManagedScopeOut[] = [],
  more: Record<string, FakeRoute> = {},
) {
  const shell = renderShell(PATH, {
    routes: {
      "GET /api/v1/access/status": { status: 200, body: { scopes, overrides: [] } },
      "GET /api/v1/environments": {
        status: 200,
        body: [anEnvironment("dev"), anEnvironment("prod")],
      },
      "GET /api/v1/access/export": { status: 200, body: { text: "version: 1\n" } },
      ...more,
    },
  });
  await screen.findByRole("heading", { level: 2, name: "Managed scopes" });
  await shell.user.click(await screen.findByRole("button", { name: "Download access file" }));
  const dialog = await screen.findByRole("dialog", { name: "Download access file" });
  return { ...shell, dialog };
}

/**
 * The export requests' paths.
 *
 * @param sent - The requests.
 * @returns Their paths.
 */
function exports(sent: readonly { path: string }[]): string[] {
  return sent
    .map((request) => request.path)
    .filter((path) => path.startsWith("/api/v1/access/export"));
}

/**
 * Everything a storage holds, as one text.
 *
 * @param storage - The storage.
 * @returns Its keys and values.
 */
function contents(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, index) => {
    const key = storage.key(index) ?? "";
    return `${key}=${storage.getItem(key) ?? ""}`;
  }).join("\n");
}

afterEach(() => {
  vi.mocked(saveBlob).mockReset();
});

describe("DownloadAccessFileDialog", () => {
  it("downloads the whole instance", async () => {
    const { user, dialog, server } = await openDialog();

    await user.click(within(dialog).getByRole("button", { name: "Download" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(exports(server.sent)).toEqual(["/api/v1/access/export?scope=instance"]);
    const [blob, name] = vi.mocked(saveBlob).mock.calls[0] ?? [];
    expect(name).toBe("access.yaml");
    expect(blob?.type).toBe("application/yaml");
    expect(await blob?.text()).toBe("version: 1\n");
    expect(screen.getByText("Downloaded access.yaml.")).toBeInTheDocument();
  });

  it("downloads some environments", async () => {
    const { user, dialog, server } = await openDialog();

    await user.click(within(dialog).getByRole("radio", { name: "Some environments" }));
    const download = within(dialog).getByRole("button", { name: "Download" });
    expect(download).toBeDisabled();
    expect(download).toHaveAccessibleDescription("Choose at least one environment.");
    const dev = await within(dialog).findByRole("checkbox", { name: "dev" });
    await user.click(dev);
    await user.click(dev);
    expect(download).toBeDisabled();
    await user.click(dev);
    await user.click(within(dialog).getByRole("checkbox", { name: "prod" }));
    expect(dev).toBeChecked();
    await user.click(download);

    await waitFor(() => {
      expect(exports(server.sent)).toEqual(["/api/v1/access/export?scope=dev&scope=prod"]);
    });
  });

  it("names the file after the managed file, safely", async () => {
    const { user, dialog } = await openDialog([managed("instance", "../../etc/x.yaml")]);

    await user.click(within(dialog).getByRole("button", { name: "Download" }));

    await waitFor(() => {
      expect(vi.mocked(saveBlob).mock.calls[0]?.[1]).toBe("x.yaml");
    });
    expect(exportName(["dev", "prod"], [managed("prod", "team.yaml")])).toBe("access.yaml");
    expect(exportName(["prod"], [managed("prod", "team.yaml")])).toBe("team.yaml");
    expect(exportName(["dev"], [managed("prod", "team.yaml")])).toBe("access.yaml");
  });

  it("keeps a refused export in the dialog", async () => {
    const { user, dialog } = await openDialog([], {
      "GET /api/v1/access/export": {
        status: 422,
        body: { detail: UNWRITABLE, type: "ValidationError" },
      },
    });

    await user.click(within(dialog).getByRole("button", { name: "Download" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(UNWRITABLE);
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("the exported text is kept nowhere", async () => {
    const logged: unknown[] = [];
    for (const method of ["log", "info", "warn", "error", "debug"] as const) {
      vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
        logged.push(args);
      });
    }
    const {
      user,
      dialog,
      storage: shellStorage,
    } = await openDialog([], {
      "GET /api/v1/access/export": { status: 200, body: { text: "# sentinel-7Qx\n" } },
    });

    await user.click(within(dialog).getByRole("button", { name: "Download" }));

    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalled();
    });
    expect(document.documentElement.outerHTML).not.toContain("sentinel-7Qx");
    for (const storage of [localStorage, sessionStorage, shellStorage]) {
      expect(contents(storage)).not.toContain("sentinel-7Qx");
    }
    expect(window.location.href).not.toContain("sentinel-7Qx");
    expect(JSON.stringify(logged)).not.toContain("sentinel-7Qx");
    vi.restoreAllMocks();
  });

  it("says when the environments cannot be read", async () => {
    const { user, dialog } = await openDialog([], {
      "GET /api/v1/environments": { status: 500, body: { detail: "The database is down." } },
    });

    await user.click(within(dialog).getByRole("radio", { name: "Some environments" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The database is down.");
  });

  it("keeps the dialog open while exporting, and says while it reads the environments", async () => {
    const answer = held();
    const environments = held();
    const { user, dialog } = await openDialog([], {
      "GET /api/v1/access/export": answer.route,
      "GET /api/v1/environments": environments.route,
    });

    await user.click(within(dialog).getByRole("radio", { name: "Some environments" }));
    expect(within(dialog).getByText("Reading the environments…")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("radio", { name: "The whole instance" }));
    await user.click(within(dialog).getByRole("button", { name: "Download" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Download access file" })).toBeInTheDocument();
    answer.release({ status: 200, body: { text: "version: 1\n" } });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    environments.release({ status: 200, body: [] });
  });

  it("Escape returns the focus to Download access file", async () => {
    const { user } = await openDialog();

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Download access file" })).toHaveFocus();
    });
  });
});
