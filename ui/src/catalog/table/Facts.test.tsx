import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ArtifactOut, TableOut } from "../../api";
import { artifact, artifactRoutes } from "../../test/artifacts";
import { renderShell } from "../../test/render";
import { table, TABLE_PATH } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const ENTRY_PATH = "/api/v1/environments/prod/namespaces/ml/tables/model";
const ARTIFACT_READ = "/api/v1/environments/prod/namespaces/ml/artifacts/model";

/**
 * The facts list, once the page has arrived.
 *
 * @returns The list's element.
 */
async function facts(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 2, name: "Facts" });
  return (heading.parentElement as HTMLElement).querySelector("dl") as HTMLElement;
}

/**
 * The words of one fact.
 *
 * @param list - The facts list.
 * @param term - The fact's name.
 * @returns The text of its answer, with the words a screen reader hears.
 */
function fact(list: HTMLElement, term: string): HTMLElement {
  const name = within(list).getByText(term, { selector: "dt" });
  return name.nextElementSibling as HTMLElement;
}

describe("Facts", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: Date.parse("2026-11-13T09:30:00Z"), toFake: ["Date"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows owner, rows, size with files, last write, last read, created and grain", async () => {
    renderShell(PAGE);

    const list = await facts();
    expect(fact(list, "Owner")).toHaveTextContent("ana.nowak");
    expect(fact(list, "Rows")).toHaveTextContent("1.2M");
    expect(fact(list, "Size")).toHaveTextContent("3.1 MB in 12 files");
    expect(fact(list, "Last write")).toHaveTextContent("1 day ago");
    expect(fact(list, "Last read")).toHaveTextContent("2 hours ago");
    expect(fact(list, "Created")).toHaveTextContent("1 month ago");
    expect(fact(list, "Grain")).toHaveTextContent("one row per sale");
  });

  it("says one file in the singular", async () => {
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ file_count: 1 }) } },
    });

    expect(fact(await facts(), "Size")).toHaveTextContent("3.1 MB in 1 file");
  });

  it("shows the size alone when the files are not counted", async () => {
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ file_count: null }) } },
    });

    expect(fact(await facts(), "Size").textContent).toBe("3.1 MB");
  });

  it("counts the readers of the last 7 days and never names them", async () => {
    renderShell(PAGE, { me: { name: "kim.schulz", admin: false } });

    const last = fact(await facts(), "Last read");
    expect(last).toHaveTextContent("3 readers in the last 7 days");
    expect(screen.queryByRole("link", { name: /readers/i })).not.toBeInTheDocument();
    expect(screen.queryByText("kim.schulz")).not.toBeInTheDocument();
  });

  it("gives an administrator Readers › to the names on the Health tab", async () => {
    renderShell(PAGE);

    const last = fact(await facts(), "Last read");
    expect(last).toHaveTextContent("3 readers in the last 7 days");
    expect(within(last).getByRole("link", { name: "Readers ›" })).toHaveAttribute(
      "href",
      `${PAGE}/health#readers`,
    );
  });

  it("says one reader in the singular", async () => {
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ readers_7d: 1 }) } },
    });

    expect(fact(await facts(), "Last read")).toHaveTextContent("1 reader in the last 7 days");
  });

  it("says Never read without a count when nobody read it", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ last_read_at: null, readers_7d: 0 }),
        },
      },
    });

    const last = fact(await facts(), "Last read");
    expect(last).toHaveTextContent("Never read");
    expect(last).not.toHaveTextContent("readers");
  });

  it("says what is not known in every fact", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({
            kind: "VIEW",
            owner: null,
            row_count: null,
            size_bytes: null,
            file_count: null,
            last_write_at: null,
            last_read_at: null,
            readers_7d: 0,
            created_at: null,
            model: null,
          }),
        },
      },
    });

    const list = await facts();
    expect(fact(list, "Owner")).toHaveTextContent("No owner");
    expect(fact(list, "Rows")).toHaveTextContent("Not known");
    expect(fact(list, "Size")).toHaveTextContent("Not known");
    expect(fact(list, "Last write")).toHaveTextContent("Never written");
    expect(fact(list, "Last read")).toHaveTextContent("Never read");
    expect(fact(list, "Created")).toHaveTextContent("Not known");
    expect(within(list).queryByText("Grain")).not.toBeInTheDocument();
    expect(within(list).queryByText(/Last write by/)).not.toBeInTheDocument();
  });

  describe("of an artifact", () => {
    const ARTIFACT_PAGE = "/catalog/prod/ml/model";

    /**
     * Renders the artifact's page with the artifact and its table entry changed.
     *
     * @param changed - The artifact's fields that differ.
     * @param expiresAt - When the entry expires; null for never.
     */
    function renderArtifact(changed: Partial<ArtifactOut> = {}, expiresAt: string | null = null) {
      const routes = artifactRoutes();
      const entry = routes[`GET ${ENTRY_PATH}`] as { status: number; body: TableOut };
      renderShell(ARTIFACT_PAGE, {
        routes: {
          ...routes,
          [`GET ${ENTRY_PATH}`]: { status: 200, body: { ...entry.body, expires_at: expiresAt } },
          [`GET ${ARTIFACT_READ}`]: { status: 200, body: artifact(changed) },
        },
      });
    }

    it("shows an artifact's current version, versions, keeps, size and expiry, and no rows", async () => {
      renderArtifact({ keep_versions: 5, size_bytes: 3_100_000 }, "2026-11-20T09:30:00Z");

      const list = await facts();
      expect(fact(list, "Owner")).toHaveTextContent("ana.nowak");
      expect(fact(list, "Current version")).toHaveTextContent("3");
      expect(fact(list, "Versions")).toHaveTextContent("3, deleted ones included");
      expect(fact(list, "Keeps")).toHaveTextContent("The newest 5 versions (keep_versions)");
      expect(fact(list, "Size")).toHaveTextContent("3.1 MB of its live versions");
      // A time to come, so the date itself: `When` words only the past.
      expect(fact(list, "Expires")).toHaveTextContent("Nov 20, 2026");
      expect(within(fact(list, "Expires")).getByText(/Nov 20, 2026/)).toHaveAttribute(
        "datetime",
        "2026-11-20T09:30:00Z",
      );
      expect(fact(list, "Created")).toHaveTextContent("1 month ago");
      expect(within(list).queryByText("Rows", { selector: "dt" })).not.toBeInTheDocument();
      expect(within(list).queryByText("Grain", { selector: "dt" })).not.toBeInTheDocument();
      // Readers › would open a Health tab the artifact does not have.
      expect(within(list).queryByText("Last read", { selector: "dt" })).not.toBeInTheDocument();
      expect(within(list).queryByText("Last write", { selector: "dt" })).not.toBeInTheDocument();
      expect(
        within(list)
          .getAllByRole("term")
          .map((term) => term.textContent),
      ).toEqual(["Owner", "Current version", "Versions", "Keeps", "Size", "Expires", "Created"]);
    });

    it("says None: every version is deleted when current_version is null", async () => {
      renderArtifact({ current_version: null, size_bytes: 0 });

      const list = await facts();
      expect(fact(list, "Current version")).toHaveTextContent("None: every version is deleted");
      expect(within(list).queryByText("Expires", { selector: "dt" })).not.toBeInTheDocument();
    });

    it("says Every version without keep_versions, and one version in the singular", async () => {
      renderArtifact({ versions: 1 });

      const list = await facts();
      expect(fact(list, "Keeps")).toHaveTextContent("Every version");
      expect(fact(list, "Versions")).toHaveTextContent("1, deleted ones included");
    });

    it("keeps the newest one version in the singular", async () => {
      renderArtifact({ keep_versions: 1 });

      expect(fact(await facts(), "Keeps")).toHaveTextContent("The newest version (keep_versions)");
    });
  });

  it("leaves Grain out when the table's header gives none", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ model: { ...(table().model as object), grain: null } as never }),
        },
      },
    });

    expect(within(await facts()).queryByText("Grain")).not.toBeInTheDocument();
  });
});
