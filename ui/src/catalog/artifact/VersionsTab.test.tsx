import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { artifactRoutes, deletedVersion, version, WEIGHTS } from "../../test/artifacts";
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
      "Files",
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
    const GONE =
      "Version 3 of ml.model was deleted; its bytes are gone. Read a version that exists: `lakana artifact versions list ml.model --env prod` shows which.";
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
});
