import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderShell } from "../../test/render";
import { permissions, table, TABLE, TABLE_PATH } from "../../test/table";
import { columnTexts, sortBy, sortState } from "../../test/sorting";
import { columnJoins, columnRole } from "./ColumnsTab";

const PAGE = "/catalog/prod/gold.sales/fact_sales/columns";
const PERMISSIONS = "GET /api/v1/principals/admin/permissions";
const READ_ONLY = { status: 200, body: permissions("admin", ["READ_TABLE", "SUBMIT_QUERY"]) };
const STORE_ID = `PUT ${TABLE_PATH}/columns/store_id/description`;

/**
 * The table with one column's texts changed.
 *
 * @param name - The column.
 * @param changes - The fields that differ.
 * @returns The table.
 */
function withColumn(name: string, changes: Partial<(typeof TABLE.schema)[number]>) {
  return table({
    schema: TABLE.schema.map((column) =>
      column.name === name ? { ...column, ...changes } : column,
    ),
  });
}

/**
 * The columns table's rows, once the page has arrived.
 *
 * @returns The rows without the header.
 */
async function rows(): Promise<HTMLElement[]> {
  const region = await screen.findByRole("region", { name: "Table columns" });
  return within(region).getAllByRole("row").slice(1);
}

describe("columnRole", () => {
  it("is key for a key column, measure for a measure column, else null", () => {
    expect(columnRole(TABLE, "sale_id")).toBe("key");
    expect(columnRole(TABLE, "amount")).toBe("measure");
    expect(columnRole(TABLE, "note")).toBeNull();
  });

  it("is null for a table without a model", () => {
    expect(columnRole(table({ model: null }), "sale_id")).toBeNull();
  });
});

describe("columnJoins", () => {
  it("lists the references of the column", () => {
    expect(columnJoins(TABLE, "store_id").map((join) => join.target)).toEqual(["gold.dim_store"]);
    expect(columnJoins(TABLE, "sale_id")).toEqual([]);
  });

  it("is empty for a table without a model", () => {
    expect(columnJoins(table({ model: null }), "store_id")).toEqual([]);
  });
});

describe("ColumnsTab", () => {
  it("shows every column with type, nullability, role, joins, description and terms", async () => {
    const schema = TABLE.schema.map((column) =>
      column.name === "sale_id"
        ? { ...column, description: "The sale's id.", terms: [{ key: "t1", name: "Sale" }] }
        : column,
    );
    renderShell(PAGE, {
      routes: {
        [PERMISSIONS]: READ_ONLY,
        [`GET ${TABLE_PATH}`]: { status: 200, body: table({ schema }) },
      },
    });

    const all = await rows();
    expect(all).toHaveLength(7);
    const cells = (row: HTMLElement): (string | null)[] =>
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent);
    expect(cells(all[0] as HTMLElement)).toEqual([
      "sale_id",
      "long",
      "Never",
      "Key",
      "",
      "The sale's id.",
      "Sale",
    ]);
    expect(cells(all[1] as HTMLElement).slice(0, 4)).toEqual(["store_id", "long", "Allowed", ""]);
    expect(cells(all[1] as HTMLElement)[4]).toContain("gold.dim_store.store_id");
    expect(cells(all[3] as HTMLElement)[3]).toBe("Measure");
  });

  it("sorts the columns by each fact but the description", async () => {
    const schema = TABLE.schema.map((column) =>
      column.name === "sale_id" ? { ...column, terms: [{ key: "t1", name: "Sale" }] } : column,
    );
    const { user } = renderShell(PAGE, {
      routes: {
        [PERMISSIONS]: READ_ONLY,
        [`GET ${TABLE_PATH}`]: { status: 200, body: table({ schema }) },
      },
    });
    await rows();
    const region = screen.getByRole("region", { name: "Table columns" });
    const names = (): (string | null)[] => columnTexts(region);
    const own = names();

    await sortBy(user, region, "Name");
    expect(names()).toEqual([...own].sort());
    await sortBy(user, region, "Nulls");
    expect(sortState(region, "Nulls")).toBe("ascending");
    expect(columnTexts(region, 2).indexOf("Allowed")).toBeGreaterThan(
      columnTexts(region, 2).lastIndexOf("Never"),
    );
    await sortBy(user, region, "Role");
    expect(columnTexts(region, 3).slice(0, 2)).toEqual(["Key", "Measure"]);
    await sortBy(user, region, "Joins");
    expect(columnTexts(region, 4)[0]).toContain("gold.dim_store.store_id");
    await sortBy(user, region, "Type");
    await sortBy(user, region, "Terms");
    expect(columnTexts(region, 6)[0]).toBe("Sale");
    expect(within(region).queryByRole("button", { name: "Description" })).toBeNull();
  });

  it("links a join to the table it points at when that exists", async () => {
    renderShell(PAGE);

    await rows();

    expect(screen.getByRole("link", { name: "gold.dim_store.store_id" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold/dim_store",
    );
  });

  it("names a join whose target does not exist in plain text", async () => {
    const model = {
      ...(TABLE.model as NonNullable<typeof TABLE.model>),
      references: [
        {
          ...(TABLE.model?.references[0] as NonNullable<typeof TABLE.model>["references"][number]),
          target_exists: false,
        },
      ],
    };
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ model }) } },
    });

    await rows();

    expect(screen.getByText("gold.dim_store.store_id")).toBeVisible();
    expect(screen.queryByRole("link", { name: "gold.dim_store.store_id" })).not.toBeInTheDocument();
  });

  it("shows a column with a role, a description and terms as plain text", async () => {
    const schema = [
      {
        ...(TABLE.schema[0] as (typeof TABLE.schema)[number]),
        description: "<b>bold</b>",
        terms: [
          { key: "a", name: "Sale" },
          { key: "b", name: "Revenue" },
        ],
      },
    ];
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ schema }) } },
    });

    const [row] = await rows();

    expect(row).toHaveTextContent("<b>bold</b>");
    expect(row?.querySelector("b")).toBeNull();
    expect(row).toHaveTextContent("Sale, Revenue");
  });

  it("says a table has no columns", async () => {
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ schema: [] }) } },
    });

    expect(await screen.findByText("This table has no columns.")).toBeVisible();
  });

  it("scrolls a wide schema in its own region", async () => {
    renderShell(PAGE);

    const region = await screen.findByRole("region", { name: "Table columns" });

    expect(region).toHaveAttribute("tabindex", "0");
    expect(within(region).getByRole("table")).toBeInTheDocument();
  });

  it("reads nothing but the table", async () => {
    const { server } = renderShell(PAGE);
    await rows();

    expect(
      server.sent.filter(
        (request) => request.path.includes("/health") || request.path.includes("lineage"),
      ),
    ).toEqual([]);
  });

  it("marks hidden characters in a column's name, its label and a join", async () => {
    const base = withColumn("channel", {
      name: "chan\u202Enel",
      label: "Chan\u2066nel",
      label_source: "edited",
      label_language: "en",
    });
    const model = base.model as NonNullable<typeof base.model>;
    const body = {
      ...base,
      model: {
        ...model,
        references: model.references.map((reference) => ({
          ...reference,
          target: "gold.dim_\u202Estore",
        })),
      },
    };
    renderShell(PAGE, { routes: { [`GET ${TABLE_PATH}`]: { status: 200, body } } });

    const region = await screen.findByRole("region", { name: "Table columns" });
    expect(
      within(region).getByText("U+2066", { selector: ".column-label > .char-mark" }),
    ).toBeVisible();
    expect(region).toHaveTextContent("ChanU+2066nel");
    expect(region).toHaveTextContent("chanU+202Enel");
    expect(
      within(region).getByRole("link", { name: "gold.dim_U+202Estore.store_id" }),
    ).toBeInTheDocument();
    // Edit and Back to Git follow once the person's rights are read.
    expect(
      await within(region).findByRole("button", {
        name: "Back to the text from Git for chanU+202Enel",
      }),
    ).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Edit chanU+202Enel" })).toBeInTheDocument();
    expect(region.textContent).not.toMatch(/[\u202E\u2066]/u);
  });

  it("marks a hidden character in a column's type, such as a struct's field name", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: withColumn("channel", { type: "struct<1: chan\u202Enel: optional string>" }),
        },
      },
    });

    const region = await screen.findByRole("region", { name: "Table columns" });
    const type = within(region).getByText("U+202E", { selector: ".column-type > .char-mark" });
    expect(type.parentElement).toHaveTextContent("struct<1: chanU+202Enel: optional string>");
    expect(region.textContent).not.toMatch(/\u202E/u);
  });

  it("marks a hidden character in a column's description", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: withColumn("channel", { description: "Sold through analy\u202Etics." }),
        },
      },
    });

    const region = await screen.findByRole("region", { name: "Table columns" });
    expect(region).toHaveTextContent("Sold through analyU+202Etics.");
    expect(region.textContent).not.toMatch(/\u202E/u);
  });

  it("marks a look-alike in a column's name but not a no-break space in its description", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: withColumn("channel", {
            name: "chan\u3164nel",
            description: "Prix\u00A0: en euros",
          }),
        },
      },
    });

    const region = await screen.findByRole("region", { name: "Table columns" });
    expect(region).toHaveTextContent("chanU+3164nel");
    expect(region.textContent).toContain("Prix\u00A0: en euros");
    expect(region.textContent).not.toMatch(/\u3164/u);
  });

  it("marks a hidden character in a column's glossary terms", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: withColumn("sale_id", { terms: [{ key: "t1", name: "Sa\u202Ele" }] }),
        },
      },
    });

    const region = await screen.findByRole("region", { name: "Table columns" });
    expect(region).toHaveTextContent("SaU+202Ele");
    expect(region.textContent).not.toMatch(/\u202E/u);
  });

  it("shows a column's label under its name", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: withColumn("store_id", { label: "Store", label_language: "en" }),
        },
      },
    });

    const all = await rows();
    expect(within(all[1] as HTMLElement).getByText("Store")).toBeVisible();
  });
});

describe("ColumnsTab editing", () => {
  it("shows Edit for every column to someone who may edit", async () => {
    renderShell(PAGE);
    expect(await screen.findAllByRole("button", { name: /^Edit / })).toHaveLength(7);
  });

  it("shows no Edit to someone who may not edit", async () => {
    const { server } = renderShell(PAGE, { routes: { [PERMISSIONS]: READ_ONLY } });
    await rows();
    await waitFor(() => {
      expect(server.sent.some((request) => request.path.includes("/permissions"))).toBe(true);
    });

    expect(screen.queryByRole("button", { name: /^Edit / })).not.toBeInTheDocument();
  });

  it("shows no Edit on the first columns of Overview", async () => {
    renderShell("/catalog/prod/gold.sales/fact_sales");

    await screen.findByRole("button", { name: "Edit" });

    expect(screen.queryByRole("button", { name: "Edit sale_id" })).not.toBeInTheDocument();
  });

  it("edits a column's texts in a row under it and returns focus to its Edit", async () => {
    const saved = withColumn("store_id", {
      description: "The store.",
      description_source: "edited",
      description_language: "en",
    });
    const { server, user } = renderShell(PAGE, {
      routes: { [STORE_ID]: { status: 200, body: saved } },
    });

    await user.click(await screen.findByRole("button", { name: "Edit store_id" }));
    const form = screen.getByRole("form", { name: "Edit the texts" });
    const all = await rows();
    expect(all[2]).toContainElement(form);
    await user.type(within(form).getByRole("textbox", { name: "Description" }), "The store.");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("The store.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Edit store_id" })).toHaveFocus();
    const put = server.sent.find((request) => request.method === "PUT");
    expect(put?.path).toBe(`${TABLE_PATH}/columns/store_id/description?lang=en`);
    expect(put?.body).toEqual({ description: "The store." });
  });

  it("saves a column's label in the language it is shown in, apart from its description", async () => {
    const mixed = {
      ...withColumn("store_id", {
        description: "Die Filiale.",
        description_language: "de",
        label: "Store",
        label_source: "project",
        label_language: "en",
      }),
      language: "de",
      translations: [{ language: "de", translated: 3, total: 5 }],
    };
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: mixed },
        [STORE_ID]: { status: 200, body: mixed },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit store_id" }));
    const form = screen.getByRole("form", { name: "Edit the texts" });
    const label = within(form).getByRole("textbox", { name: "Label (English)" });
    await user.clear(label);
    await user.type(label, "Shop");
    expect(within(form).getByRole("textbox", { name: "Description (German)" })).toBeVisible();
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(server.sent.some((request) => request.method === "PUT")).toBe(true);
    });
    const put = server.sent.find((request) => request.method === "PUT");
    expect(put?.path).toBe(`${TABLE_PATH}/columns/store_id/description?lang=de`);
    expect(put?.body).toEqual({ label: "Shop", label_language: "en" });
  });

  it("goes back to the text from Git for a column, each field in the language it is shown in", async () => {
    const mixed = {
      ...withColumn("channel", {
        description: "Der Kanal.",
        description_source: "edited",
        description_language: "de",
        label: "Channel",
        label_source: "edited",
        label_language: "en",
      }),
      language: "de",
      translations: [{ language: "de", translated: 3, total: 5 }],
    };
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: mixed },
        [`PUT ${TABLE_PATH}/columns/channel/description`]: { status: 200, body: TABLE },
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Back to the text from Git for channel" }),
    );

    await waitFor(() => {
      expect(server.sent.some((request) => request.method === "PUT")).toBe(true);
    });
    const put = server.sent.find((request) => request.method === "PUT");
    expect(put?.path).toBe(`${TABLE_PATH}/columns/channel/description?lang=de`);
    expect(put?.body).toEqual({ description: null, label: null, label_language: "en" });
  });

  it("keeps one editor and its unsaved text until it is saved or cancelled", async () => {
    const { user } = renderShell(PAGE);

    await user.click(await screen.findByRole("button", { name: "Edit store_id" }));
    const form = screen.getByRole("form", { name: "Edit the texts" });
    await user.type(within(form).getByRole("textbox", { name: "Description" }), "Unsaved.");

    const other = screen.getByRole("button", { name: /^Edit sale_id/ });
    expect(other).toBeDisabled();
    expect(other).toHaveAccessibleName(/save or cancel the open editor first/);
    await user.click(other);
    expect(
      within(screen.getByRole("form")).getByRole("textbox", { name: "Description" }),
    ).toHaveValue("Unsaved.");

    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Edit sale_id" })).toBeEnabled();
  });

  it("moves focus to the column's Edit after Cancel and after Escape", async () => {
    const { user } = renderShell(PAGE);

    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Edit note" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Edit note" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit note" })).toHaveFocus();
  });

  it("goes back to the text from Git for one column and returns focus to its Edit", async () => {
    const edited = withColumn("channel", {
      description: "Edited.",
      description_source: "edited",
      description_language: "en",
    });
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: edited },
        [`PUT ${TABLE_PATH}/columns/channel/description`]: { status: 200, body: TABLE },
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Back to the text from Git for channel" }),
    );

    await waitFor(() => {
      expect(screen.queryByText("Edited.")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Edit channel" })).toHaveFocus();
    const put = server.sent.find((request) => request.method === "PUT");
    expect(put?.path).toBe(`${TABLE_PATH}/columns/channel/description?lang=en`);
    expect(put?.body).toEqual({ description: null });
    expect(screen.queryByRole("button", { name: /^Back to the text/ })).toBeNull();
  });

  it("removes a column's edited label and description together", async () => {
    const edited = withColumn("channel", {
      description: "Edited.",
      description_source: "edited",
      label: "Channel",
      label_source: "edited",
    });
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: edited },
        [`PUT ${TABLE_PATH}/columns/channel/description`]: { status: 200, body: TABLE },
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Back to the text from Git for channel" }),
    );

    await waitFor(() => {
      expect(server.sent.find((request) => request.method === "PUT")?.body).toEqual({
        description: null,
        label: null,
      });
    });
  });

  it("removes only an edited label of a column", async () => {
    const edited = withColumn("channel", { label: "Channel", label_source: "edited" });
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: { status: 200, body: edited },
        [`PUT ${TABLE_PATH}/columns/channel/description`]: { status: 200, body: TABLE },
      },
    });

    await user.click(
      await screen.findByRole("button", { name: "Back to the text from Git for channel" }),
    );

    await waitFor(() => {
      expect(server.sent.find((request) => request.method === "PUT")?.body).toEqual({
        label: null,
      });
    });
  });

  it("says a deploy keeps an edited column text", async () => {
    const edited = withColumn("channel", { description: "Edited.", description_source: "edited" });
    renderShell(PAGE, { routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: edited } } });

    expect(await screen.findByText("A deploy never replaces a text edited here.")).toBeVisible();
  });

  it("keeps the text and shows the refusal in the row", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [STORE_ID]: { status: 422, body: { detail: "Column texts may not be blank." } },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit store_id" }));
    await user.type(screen.getByRole("textbox", { name: "Description" }), "x");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Column texts may not be blank.");
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("x");
  });

  it("hides every Edit after a refusal for permission", async () => {
    let reads = 0;
    const { user } = renderShell(PAGE, {
      routes: {
        [PERMISSIONS]: () => {
          reads += 1;
          return reads === 1
            ? { status: 200, body: permissions("admin", ["READ_TABLE", "DROP_TABLE"]) }
            : READ_ONLY;
        },
        [STORE_ID]: { status: 403, body: { detail: "You may not edit this table's texts." } },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit store_id" }));
    await user.type(screen.getByRole("textbox", { name: "Description" }), "x");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You may not edit");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("button", { name: /^Edit / })).not.toBeInTheDocument();
    const all = await rows();
    expect(all[1]).toHaveFocus();
  });

  it("names the language of a column text when the table has more than one", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: table({ translations: [{ language: "de", translated: 1, total: 2 }] }),
        },
      },
    });

    await user.click(await screen.findByRole("button", { name: "Edit note" }));

    expect(screen.getByRole("textbox", { name: "Description (English)" })).toBeVisible();
  });

  it("says the page edits the main language when there is no translation", async () => {
    renderShell(PAGE, {
      routes: { [`GET ${TABLE_PATH}`]: { status: 200, body: table({ language: "de" }) } },
    });

    expect(await screen.findByText(/shows and edits the main language, English\./)).toBeVisible();
  });

  it("shows a column description that carries markup as text", async () => {
    renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}`]: {
          status: 200,
          body: withColumn("note", { description: "<img src=x onerror=alert(1)>" }),
        },
      },
    });

    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeVisible();
    expect(document.querySelector("img")).toBeNull();
  });
});
