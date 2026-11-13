import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../../test/render";
import { permissions, table, TABLE, TABLE_PATH } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";

describe("Overview", () => {
  it("shows the description and where it comes from", async () => {
    renderShell(PAGE);

    expect(await screen.findByText("Every sale in every store, one row per sale.")).toBeVisible();
    expect(screen.getByText("From the project in Git")).toBeVisible();
  });

  it("says a description was edited here", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ description_source: "edited" }),
        },
      },
    });

    expect(await screen.findByText("Edited here")).toBeVisible();
    expect(screen.queryByText("From the project in Git")).not.toBeInTheDocument();
  });

  it("says there is no description yet, without a source", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ description: null, description_source: null }),
        },
      },
    });

    expect(await screen.findByText("No description yet.")).toBeVisible();
    expect(screen.queryByText("From the project in Git")).not.toBeInTheDocument();
    expect(screen.queryByText("Edited here")).not.toBeInTheDocument();
  });

  it("shows the first five columns and links to all", async () => {
    renderShell(PAGE);

    const heading = await screen.findByRole("heading", { level: 2, name: "Columns" });
    const block = heading.parentElement as HTMLElement;
    const names = within(block)
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[0]?.textContent);
    expect(names).toEqual(["sale_id", "store_id", "sold_at", "amount", "customer_email"]);
    expect(within(block).getByRole("link", { name: "All columns ›" })).toHaveAttribute(
      "href",
      `${PAGE}/columns`,
    );
  });

  it("shows each column's type with its role", async () => {
    renderShell(PAGE);

    const heading = await screen.findByRole("heading", { level: 2, name: "Columns" });
    const rows = within(heading.parentElement as HTMLElement).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("sale_id");
    expect(rows[1]).toHaveTextContent("long");
    expect(rows[1]).toHaveTextContent("Key");
    expect(rows[4]).toHaveTextContent("decimal(10,2)");
    expect(rows[4]).toHaveTextContent("Measure");
  });

  it("does not run a preview", async () => {
    const { server } = renderShell(PAGE);
    await screen.findByText("Every sale in every store, one row per sale.");

    await waitFor(() => {
      expect(server.sent.some((request) => request.path.includes("/health"))).toBe(true);
    });
    expect(server.sent.some((request) => request.path.split("?")[0]?.endsWith("/preview"))).toBe(
      false,
    );
  });

  it("reads health without readers", async () => {
    const { server } = renderShell(PAGE);

    await screen.findByRole("heading", { level: 2, name: "Health" });

    await waitFor(() => {
      expect(
        server.sent.some((request) => request.path.endsWith("/health?days=7&readers=false")),
      ).toBe(true);
    });
    expect(server.sent.some((request) => request.path.includes("readers=true"))).toBe(false);
  });

  it("shows the sections in the order of the page", async () => {
    renderShell(PAGE);

    await screen.findByRole("heading", { level: 2, name: "Health" });
    const names = screen
      .getAllByRole("heading", { level: 2 })
      .map((heading) => heading.textContent);
    expect(names).toEqual(["Description", "Columns", "Health", "Facts", "Comes from · goes to"]);
  });
});

const PERMISSIONS = "GET /api/v1/principals/admin/permissions";
const READ_ONLY = { status: 200, body: permissions("admin", ["READ_TABLE", "SUBMIT_QUERY"]) };
const GERMAN = [{ language: "de", translated: 3, total: 5 }];
const EDITED = table({ description: "Edited text.", description_source: "edited" });

/**
 * Waits until the page has read what the person may do.
 *
 * @param server - The fake server.
 * @param server.sent - The requests so far.
 */
async function permissionsRead(server: { sent: { path: string }[] }): Promise<void> {
  await waitFor(() => {
    expect(server.sent.some((request) => request.path.includes("/permissions"))).toBe(true);
  });
  await screen.findByRole("heading", { level: 2, name: "Facts" });
}

describe("Overview editing", () => {
  it("shows Edit to someone who may drop the table", async () => {
    renderShell(PAGE);

    expect(await screen.findByRole("button", { name: "Edit" })).toBeVisible();
  });

  it("shows Edit to the owner without any permission to drop the table", async () => {
    renderShell(PAGE, {
      routes: {
        [PERMISSIONS]: READ_ONLY,
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ owner: { name: "admin", kind: "USER" } }),
        },
      },
    });

    expect(await screen.findByRole("button", { name: "Edit" })).toBeVisible();
  });

  it("shows Edit to neither the owner nor someone who may drop the table", async () => {
    const { server } = renderShell(PAGE, { routes: { [PERMISSIONS]: READ_ONLY } });

    await permissionsRead(server);

    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back to the text from Git" })).toBeNull();
  });

  it("saves the changed description in the shown language", async () => {
    const saved = table({
      description: "Every sale, from every till.",
      description_source: "edited",
    });
    const { server, user } = renderShell(PAGE, {
      routes: { [`PATCH ${TABLE_PATH}`]: { status: 200, body: saved } },
    });

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox", { name: "Description" });
    await user.clear(box);
    await user.type(box, "Every sale, from every till.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Edited here")).toBeVisible();
    expect(screen.getByText("Every sale, from every till.")).toBeVisible();
    const patch = server.sent.find((request) => request.method === "PATCH");
    expect(patch?.path).toBe(`${TABLE_PATH}?lang=en`);
    expect(patch?.body).toEqual({ description: "Every sale, from every till." });
  });

  it("saves a label, which then shows under the heading", async () => {
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`PATCH ${TABLE_PATH}`]: {
          status: 200,
          body: table({ label: "Sales", label_source: "edited", label_language: "en" }),
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox", { name: "Label" }), "Sales");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Sales")).toBeVisible();
    expect(server.sent.find((request) => request.method === "PATCH")?.body).toEqual({
      label: "Sales",
    });
  });

  it("closes without a request when nothing changed", async () => {
    const { server, user } = renderShell(PAGE);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.method === "PATCH")).toBe(false);
  });

  it("goes back to the text from Git in this language only", async () => {
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: EDITED },
        [`PATCH ${TABLE_PATH}`]: { status: 200, body: TABLE },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Back to the text from Git" }));

    expect(await screen.findByText("From the project in Git")).toBeVisible();
    const patch = server.sent.find((request) => request.method === "PATCH");
    expect(patch?.path).toBe(`${TABLE_PATH}?lang=en`);
    expect(patch?.body).toEqual({ description: null });
    expect(screen.queryByRole("button", { name: "Back to the text from Git" })).toBeNull();
  });

  it("removes every edit when it goes back: the label's as well as the description's", async () => {
    const both = table({
      description_source: "edited",
      label: "Sales",
      label_source: "edited",
      label_language: "en",
    });
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: both },
        [`PATCH ${TABLE_PATH}`]: { status: 200, body: TABLE },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Back to the text from Git" }));

    await waitFor(() => {
      expect(server.sent.find((request) => request.method === "PATCH")?.body).toEqual({
        description: null,
        label: null,
      });
    });
  });

  it("removes only an edited label when the description is the project's", async () => {
    const labelled = table({ label: "Sales", label_source: "edited", label_language: "en" });
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: labelled },
        [`PATCH ${TABLE_PATH}`]: { status: 200, body: TABLE },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Back to the text from Git" }));

    await waitFor(() => {
      expect(server.sent.find((request) => request.method === "PATCH")?.body).toEqual({
        label: null,
      });
    });
  });

  it("offers no way back for a text that is the project's", async () => {
    renderShell(PAGE);

    await screen.findByRole("button", { name: "Edit" });

    expect(screen.queryByRole("button", { name: "Back to the text from Git" })).toBeNull();
    expect(screen.queryByText(/A deploy never replaces/)).toBeNull();
  });

  it("says a deploy keeps an edited text", async () => {
    renderShell(PAGE, { routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: EDITED } } });

    expect(await screen.findByText("A deploy never replaces a text edited here.")).toBeVisible();
  });

  it("names the language when the table has more than one", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: table({ translations: GERMAN }) },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit" }));

    expect(screen.getByRole("textbox", { name: "Description (English)" })).toBeVisible();
  });

  it("does not name the language of a table with one", async () => {
    const { user } = renderShell(PAGE);

    await user.click(await screen.findByRole("button", { name: "Edit" }));

    expect(screen.getByRole("textbox", { name: "Description" })).toBeVisible();
  });

  it("says it shows and edits the main language when there is no translation", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: table({ language: "de" }) },
      },
    });

    expect(
      await screen.findByText(
        "There is no text in your language yet, so the page shows and edits the main language, English.",
      ),
    ).toBeVisible();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByRole("form", { name: "Edit the texts" })).toBeVisible();
  });

  it("does not tell someone who may not edit that the page edits the main language", async () => {
    const { server } = renderShell(PAGE, {
      routes: {
        [PERMISSIONS]: READ_ONLY,
        [`GET ${TABLE_PATH}`]: { status: 200, body: table({ language: "de" }) },
      },
    });

    await permissionsRead(server);

    expect(screen.queryByText(/shows and edits the main language/)).not.toBeInTheDocument();
  });

  it("keeps the text and shows the refusal next to the field", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`PATCH ${TABLE_PATH}`]: {
          status: 422,
          body: { detail: "The description is too long for this table." },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox", { name: "Description" }), " More.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The description is too long for this table.",
    );
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      "Every sale in every store, one row per sale. More.",
    );
  });

  it("hides Edit after a refusal for permission", async () => {
    let reads = 0;
    const { user } = renderShell(PAGE, {
      routes: {
        [PERMISSIONS]: () => {
          reads += 1;
          return reads === 1
            ? { status: 200, body: permissions("admin", ["READ_TABLE", "DROP_TABLE"]) }
            : READ_ONLY;
        },
        [`PATCH ${TABLE_PATH}`]: {
          status: 403,
          body: {
            detail: "Only the table's owner may edit its texts. Ask the owner or an administrator.",
          },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox", { name: "Description" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only the table's owner may edit");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(reads).toBe(2);
    expect(screen.getByRole("heading", { level: 2, name: "Description" })).toHaveFocus();
  });

  it("moves focus to Edit after Save, Cancel and Escape", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`PATCH ${TABLE_PATH}`]: { status: 200, body: table({ description: "Changed." }) },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox", { name: "Description" }), "!");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Changed.");
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();
  });

  it("moves focus to Edit after going back to the text from Git", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: EDITED },
        [`PATCH ${TABLE_PATH}`]: { status: 200, body: TABLE },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Back to the text from Git" }));

    await screen.findByText("From the project in Git");
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();
  });

  it("names the cause beside the button when going back fails", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: EDITED },
        [`PATCH ${TABLE_PATH}`]: {
          status: 503,
          body: { detail: "The catalog is busy. Try again." },
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Back to the text from Git" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The catalog is busy. Try again.");
    expect(screen.getByText("Edited text.")).toBeVisible();
  });

  it("shows a description that carries markup as text", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ description: "<img src=x onerror=alert(1)>" }),
        },
      },
    });

    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeVisible();
    expect(document.querySelector("img")).toBeNull();
  });
});
