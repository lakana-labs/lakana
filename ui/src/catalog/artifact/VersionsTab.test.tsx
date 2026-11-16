import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  artifact,
  artifactRoutes,
  DELETED,
  deletedVersion,
  version,
  WEIGHTS,
} from "../../test/artifacts";
import { manageRoutes } from "../../test/manage";
import { held } from "../../test/pending";
import { renderShell } from "../../test/render";

/**
 * One item of a list, which the test expects to be there.
 *
 * @param list - The list.
 * @param index - The item's place.
 * @returns The item.
 * @throws {Error} when the list is shorter.
 */
function at<T>(list: readonly T[], index: number): T {
  const item = list[index];
  if (item === undefined) {
    throw new Error(`The list has no item ${String(index)}.`);
  }
  return item;
}

const TAB = "/catalog/prod/ml/model/versions";
const VERSIONS = "GET /api/v1/environments/prod/namespaces/ml/artifacts/model/versions";
const ARTIFACT = "GET /api/v1/environments/prod/namespaces/ml/artifacts/model";
const DELETE = "DELETE /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3";
const DONE =
  "Deleted version 3 of ml.model. Freed 1 file (6 B). Kept 1 file (7 B) that version 2 also uses.";
const GONE =
  "Version 3 of ml.model was deleted; its bytes are gone. Read a version that exists: `lakana artifact versions list ml.model --env prod` shows which.";

/**
 * The versions after version 3 was deleted: 2 is the newest live one.
 *
 * @returns The answer of the versions' route.
 */
function afterDelete() {
  return {
    [VERSIONS]: {
      status: 200,
      body: [
        { ...DELETED, files: [] },
        version({ version: 2, created_at: "2026-11-07T06:00:00Z" }),
        deletedVersion(),
      ],
    },
    [ARTIFACT]: { status: 200, body: artifact({ current_version: 2 }) },
  };
}

/**
 * How many times a path was read.
 *
 * @param sent - Every request.
 * @param suffix - The end of the path.
 * @returns The count of GETs.
 */
function reads(sent: readonly { method: string; path: string }[], suffix: string): number {
  return sent.filter((request) => request.method === "GET" && request.path.endsWith(suffix)).length;
}

/**
 * The versions list's rows below its head, without the rows that hold a version's files.
 *
 * @returns The rows.
 */
async function rows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "Versions of ml.model" });
  const table = region.querySelector("table") as HTMLTableElement;
  return [...at([...table.tBodies], 0).rows].filter(
    (row) => !row.classList.contains("version-files-row"),
  );
}

/**
 * The cells of a row, each as text, with a time as its relative words.
 *
 * @param row - The row.
 * @returns The texts.
 */
function cells(row: HTMLElement): string[] {
  return [...row.querySelectorAll(":scope > td")].map((cell) => {
    const copy = cell.cloneNode(true) as HTMLElement;
    copy.querySelectorAll(".visually-hidden").forEach((hidden) => {
      hidden.remove();
    });
    const time = copy.querySelector("time");
    if (time !== null) {
      time.textContent = "WHEN";
    }
    return copy.textContent;
  });
}

describe("VersionsTab", () => {
  it("lists the versions newest first with current, stored by and when, size and files", async () => {
    renderShell(TAB, { routes: artifactRoutes() });

    expect(await screen.findByRole("heading", { level: 2, name: "Versions" })).toBeVisible();
    const listed = await rows();
    const three = at(listed, 0);
    const two = at(listed, 1);
    const one = at(listed, 2);
    expect(cells(three)).toEqual([
      "3current",
      "WHENby ana.nowakfrom commit 4f9d2a7",
      "13 B",
      "2",
      "FilesDelete…",
    ]);
    expect(cells(two)[0]).toBe("2");
    expect(within(three).getByText("current")).toBeVisible();
    expect(within(three).getByText("3").closest("td")).not.toHaveClass("number");
    expect(three.querySelectorAll("td.number")).toHaveLength(2);
    expect(one).toBeDefined();
  });

  it("leaves out who stored a version and its commit when they are not known", async () => {
    renderShell(TAB, {
      routes: {
        ...artifactRoutes(),
        [VERSIONS]: {
          status: 200,
          body: [version({ created_by: null, commit_sha: null })],
        },
      },
    });

    const listed = await rows();
    const three = at(listed, 0);
    expect(cells(three)[1]).toBe("WHEN");
  });

  it("shows a deleted version as Deleted {when} by {who}, with no buttons", async () => {
    renderShell(TAB, {
      routes: {
        ...artifactRoutes(),
        [VERSIONS]: {
          status: 200,
          body: [deletedVersion(), deletedVersion({ version: 0, deleted_by: null })],
        },
      },
    });

    const listed = await rows();
    const one = at(listed, 0);
    const zero = at(listed, 1);
    expect(cells(one)[4]).toBe("Deleted WHEN by kim.schulz");
    expect(within(one).queryByRole("button")).toBeNull();
    expect(cells(zero)[4]).toBe("Deleted WHEN");
  });

  it("shows your open uploads below the versions", async () => {
    renderShell(TAB, { routes: artifactRoutes() });

    const versions = await screen.findByRole("heading", { level: 2, name: "Versions" });
    const uploads = await screen.findByRole("heading", { level: 2, name: "Your open uploads" });
    expect(versions.compareDocumentPosition(uploads)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(
      await screen.findByRole("button", {
        name: "Withdraw upload 6f1e2d3c-4b5a-4968-8776-5a4b3c2d1e0f",
      }),
    ).toBeVisible();
  });

  it("says when the artifact has no version yet", async () => {
    renderShell(TAB, { routes: { ...artifactRoutes(), [VERSIONS]: { status: 200, body: [] } } });

    expect(
      await screen.findByText("ml.model has no version yet; run the asset that produces it."),
    ).toBeVisible();
    expect(screen.queryByRole("region", { name: "Versions of ml.model" })).toBeNull();
  });

  it("shows 100 versions, then 100 more with the focus on the first new one", async () => {
    const many = Array.from({ length: 230 }, (_unused, index) => version({ version: 230 - index }));
    const { user } = renderShell(TAB, {
      routes: { ...artifactRoutes(), [VERSIONS]: { status: 200, body: many } },
    });

    expect(await rows()).toHaveLength(100);
    await user.click(screen.getByRole("button", { name: "Show 100 more" }));

    const shown = await rows();
    expect(shown).toHaveLength(200);
    expect(document.activeElement).toBe(at(shown, 100).querySelector(".version-number"));
    expect(document.activeElement).toHaveTextContent("130");

    await user.click(screen.getByRole("button", { name: "Show 30 more" }));

    const all = await rows();
    expect(all).toHaveLength(230);
    expect(document.activeElement).toHaveTextContent("30");
    expect(document.activeElement).toBe(at(all, 200).querySelector(".version-number"));
    expect(screen.queryByRole("button", { name: /^Show \d+ more$/ })).toBeNull();
  });

  it("opens one version's files at a time with aria-expanded", async () => {
    const { user } = renderShell(TAB, { routes: artifactRoutes() });

    const listed = await rows();
    const three = at(listed, 0);
    const two = at(listed, 1);
    const filesOfThree = within(three).getByRole("button", { name: "Files of version 3" });
    const filesOfTwo = within(two).getByRole("button", { name: "Files of version 2" });
    expect(filesOfThree).toHaveAttribute("aria-expanded", "false");

    await user.click(filesOfThree);

    expect(filesOfThree).toHaveAttribute("aria-expanded", "true");
    const files = await screen.findByRole("region", { name: "Files of version 3" });
    expect(
      document.getElementById(filesOfThree.getAttribute("aria-controls") ?? ""),
    ).toContainElement(files);
    expect(await within(files).findByText("config.json")).toBeVisible();

    await user.click(filesOfTwo);

    expect(filesOfThree).toHaveAttribute("aria-expanded", "false");
    expect(filesOfTwo).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByRole("region", { name: "Files of version 3" })).toBeNull();
    expect(filesOfTwo).toHaveFocus();

    await user.click(filesOfTwo);

    expect(filesOfTwo).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: /^Files of version/ })).toBeNull();
  });

  it("shows ReadFailed with Try again when the versions cannot be read", async () => {
    const { server, user } = renderShell(TAB, {
      routes: {
        ...artifactRoutes(),
        [VERSIONS]: { status: 500, body: { detail: "The catalog database broke." } },
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The catalog database broke.");
    server.route(artifactRoutes());

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await rows()).toHaveLength(3);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says a version is gone where it stood, with the server's sentence, after a download finds it deleted", async () => {
    const { server, user } = renderShell(TAB, { routes: artifactRoutes() });

    const listed = await rows();
    const three = at(listed, 0);
    await user.click(within(three).getByRole("button", { name: "Files of version 3" }));
    server.route({
      "GET /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3/blobs/9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c":
        { status: 404, body: { detail: GONE } },
      [VERSIONS]: {
        status: 200,
        body: [
          version({ deleted_at: "2026-11-16T09:00:00Z", deleted_by: "kim.schulz", actions: [] }),
        ],
      },
    });
    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Files of version 3" })).toBeNull();
    });
    const deleted = at(await rows(), 0);
    expect(cells(deleted)[4]).toBe(`Deleted WHEN by kim.schulz${GONE}`);
    expect(within(deleted).getByText(GONE)).toHaveFocus();
    expect(
      server.sent.filter(
        (request) => request.method === "GET" && request.path.endsWith("/model/versions"),
      ),
    ).toHaveLength(2);
  });

  it("keeps the versions shown and says why when reading them again fails", async () => {
    const { server, user } = renderShell(TAB, { routes: artifactRoutes() });

    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Files of version 3" }),
    );
    server.route({
      [`GET /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3/blobs/${WEIGHTS.sha256}`]:
        { status: 404, body: { detail: "ml.model has no version 3." } },
      [VERSIONS]: { status: 500, body: { detail: "The catalog database broke." } },
    });
    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));

    expect(await screen.findByText("The catalog database broke.")).toHaveAttribute("role", "alert");
    expect(await rows()).toHaveLength(3);
  });

  it("offers Delete only when the version's actions list delete", async () => {
    renderShell(TAB, {
      routes: {
        ...artifactRoutes(),
        [VERSIONS]: {
          status: 200,
          body: [version(), version({ version: 2, actions: [] }), deletedVersion()],
        },
      },
    });

    const listed = await rows();
    expect(within(at(listed, 0)).getByRole("button", { name: "Delete version 3…" })).toBeVisible();
    expect(
      within(at(listed, 0)).getByRole("button", { name: "Delete version 3…" }),
    ).toHaveTextContent("Delete…");
    expect(within(at(listed, 1)).queryByRole("button", { name: /^Delete/ })).toBeNull();
    expect(within(at(listed, 1)).getByRole("button", { name: "Files of version 2" })).toBeVisible();
    expect(within(at(listed, 2)).queryByRole("button")).toBeNull();
  });

  it("disables a git-owned Drop and still offers Delete on its versions", async () => {
    const { user } = renderShell(TAB, {
      routes: {
        ...artifactRoutes(),
        [ARTIFACT]: {
          status: 200,
          body: artifact({ defined_by: { repository: "analytics", path: "assets/ml/model.py" } }),
        },
      },
    });

    expect(
      within(at(await rows(), 0)).getByRole("button", { name: "Delete version 3…" }),
    ).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Actions" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Drop…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(menu).toHaveAccessibleDescription(
      "Defined in analytics/assets/ml/model.py — change it in git",
    );
  });

  it("shows what was freed and kept, then Close reads the versions and the artifact again", async () => {
    const { server, user } = renderShell(TAB, {
      routes: { ...artifactRoutes(), ...manageRoutes() },
    });

    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Delete version 3…" }),
    );
    await user.click(await screen.findByRole("button", { name: "Delete the version" }));
    expect(await screen.findByText(DONE)).toBeVisible();
    expect(reads(server.sent, "/model/versions")).toBe(1);
    expect(reads(server.sent, "/artifacts/model")).toBe(1);
    server.route(afterDelete());

    await user.click(screen.getByRole("button", { name: "Close" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(async () => {
      expect(cells(at(await rows(), 0))[4]).toBe("Deleted WHEN by admin");
    });
    expect(reads(server.sent, "/model/versions")).toBe(2);
    expect(reads(server.sent, "/artifacts/model")).toBe(2);
    expect(document.activeElement).toBe(at(await rows(), 0).querySelector(".version-number"));
  });

  it("reloads the artifact after deleting the current version, so current moves", async () => {
    const { server, user } = renderShell(TAB, {
      routes: { ...artifactRoutes(), ...manageRoutes() },
    });

    expect(cells(at(await rows(), 0))[0]).toBe("3current");
    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Delete version 3…" }),
    );
    await user.click(await screen.findByRole("button", { name: "Delete the version" }));
    await screen.findByText(DONE);
    server.route(afterDelete());
    await user.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(async () => {
      expect(cells(at(await rows(), 1))[0]).toBe("2current");
    });
    expect(cells(at(await rows(), 0))[0]).toBe("3");
  });

  it("Cancel returns the focus to the version's Delete", async () => {
    const { server, user } = renderShell(TAB, {
      routes: { ...artifactRoutes(), ...manageRoutes() },
    });

    const remove = within(at(await rows(), 0)).getByRole("button", { name: "Delete version 3…" });
    await user.click(remove);
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(remove).toHaveFocus();
    });
    expect(server.sent.filter((request) => request.method === "DELETE")).toEqual([]);
  });

  it("reads the versions again when the version was deleted meanwhile, and Cancel then gives the focus to its row", async () => {
    const detail = "Version 3 of ml.model is already deleted.";
    const { server, user } = renderShell(TAB, {
      routes: {
        ...artifactRoutes(),
        ...manageRoutes(),
        [DELETE]: { status: 409, body: { type: "ConflictError", detail } },
      },
    });

    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Delete version 3…" }),
    );
    server.route(afterDelete());
    await user.click(await screen.findByRole("button", { name: "Delete the version" }));

    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent(detail);
    await waitFor(() => {
      expect(reads(server.sent, "/model/versions")).toBe(2);
    });
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    const first = at(await rows(), 0);
    expect(cells(first)[4]).toBe("Deleted WHEN by admin");
    await waitFor(() => {
      expect(first.querySelector(".version-number")).toHaveFocus();
    });
  });

  it("forgets a download's gone sentence once the versions show the version live, so a later delete shows only its own words", async () => {
    const { server, user } = renderShell(TAB, {
      routes: { ...artifactRoutes(), ...manageRoutes() },
    });

    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Files of version 3" }),
    );
    server.route({
      [`GET /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3/blobs/${WEIGHTS.sha256}`]:
        { status: 404, body: { detail: GONE } },
    });
    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));
    // The versions read again still show version 3 live: the sentence stays beside the file.
    await waitFor(() => {
      expect(reads(server.sent, "/model/versions")).toBe(2);
    });
    expect(await screen.findByText(GONE)).toBeVisible();

    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Delete version 3…" }),
    );
    await user.click(await screen.findByRole("button", { name: "Delete the version" }));
    await screen.findByText(DONE);
    server.route(afterDelete());
    await user.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(async () => {
      expect(cells(at(await rows(), 0))[4]).toBe("Deleted WHEN by admin");
    });
    expect(screen.queryByText(GONE)).toBeNull();
    expect(document.activeElement).toBe(at(await rows(), 0).querySelector(".version-number"));
  });

  it("forgets a download's gone sentence when Delete… opens on that version before the versions are read again", async () => {
    const reread = held();
    const { server, user } = renderShell(TAB, {
      routes: { ...artifactRoutes(), ...manageRoutes() },
    });

    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Files of version 3" }),
    );
    server.route({
      [`GET /api/v1/environments/prod/namespaces/ml/artifacts/model/versions/3/blobs/${WEIGHTS.sha256}`]:
        { status: 404, body: { detail: GONE } },
      [VERSIONS]: reread.route,
    });
    await user.click(await screen.findByRole("button", { name: "Download models/model.pkl" }));
    expect(await screen.findByText(GONE)).toBeVisible();

    await user.click(
      within(at(await rows(), 0)).getByRole("button", { name: "Delete version 3…" }),
    );
    const dialog = await screen.findByRole("dialog");
    reread.release(afterDelete()[VERSIONS]);

    // The open dialog hides the page from the accessibility tree, so the row is read from the DOM.
    await waitFor(() => {
      const first = document.querySelector<HTMLElement>(".versions-scroll tbody tr");
      expect(cells(first as HTMLElement)[4]).toBe("Deleted WHEN by admin");
    });
    expect(screen.queryByText(GONE)).toBeNull();
    expect(dialog).toBeInTheDocument();
  });
});
