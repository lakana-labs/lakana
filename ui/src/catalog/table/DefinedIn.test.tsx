import { cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderShell } from "../../test/render";
import { DEFINITION, definitionFile, TABLE_PATH } from "../../test/table";

const PAGE = "/catalog/prod/gold.sales/fact_sales";
const FILE_PATH = `${TABLE_PATH}/definition/file`;

/** Every character the display rule marks, as a pattern; written with escapes only. */
// Matching control characters is the point of this pattern.
// eslint-disable-next-line no-control-regex
const RAW = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u2028\u2029\p{Cf}]/u;

/**
 * The answer of Defined in, once it arrived.
 *
 * @returns The `<dd>` of the fact.
 */
async function definedIn(): Promise<HTMLElement> {
  const term = await screen.findByText("Defined in", { selector: "dt" });
  return term.nextElementSibling as HTMLElement;
}

/**
 * Opens the file's dialog with the path's button.
 *
 * @param user - The user-event session.
 * @returns The dialog.
 */
async function openFile(user: ReturnType<typeof renderShell>["user"]): Promise<HTMLElement> {
  const fact = await definedIn();
  await user.click(within(fact).getByRole("button"));
  return screen.findByRole("dialog");
}

/**
 * The file's text in the dialog, once it arrived.
 *
 * @param dialog - The dialog.
 * @returns The scrolling region that holds the text.
 */
function fileText(dialog: HTMLElement): Promise<HTMLElement> {
  return within(dialog).findByRole("region", { name: "The file's text" });
}

describe("DefinedIn", () => {
  // jsdom lays nothing out and has no scrollIntoView; the spy records what was scrolled to.
  const scrolled = vi.fn();
  beforeEach(() => {
    scrolled.mockClear();
    Element.prototype.scrollIntoView = scrolled;
  });
  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  });

  it("shows where the table is defined at the deployed commit", async () => {
    renderShell(PAGE);

    const fact = await definedIn();
    expect(fact).toHaveTextContent("models/gold/fact_sales.sql, line 1 · a1b2c3d");
    expect(fact).not.toHaveTextContent("a1b2c3d4");
  });

  it("leaves out Defined in for a table no project defines", async () => {
    const { server } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}/definition`]: {
          status: 200,
          body: {
            defined: false,
            repository: null,
            path: null,
            commit: null,
            line: null,
            project_version_id: null,
            deployed_at: null,
            browse_url: null,
          },
        },
      },
    });

    await screen.findByText("Grain", { selector: "dt" });
    await waitFor(() => {
      expect(server.sent.some((request) => request.path.endsWith("/definition"))).toBe(true);
    });
    expect(screen.queryByText("Defined in")).not.toBeInTheDocument();
  });

  it("says why it cannot say where the table is defined, and reads again on Try again", async () => {
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}/definition`]: {
          status: 403,
          body: { detail: "You may not read gold.sales.fact_sales." },
        },
      },
    });

    const fact = await definedIn();
    expect(await within(fact).findByRole("alert")).toHaveTextContent(
      "You may not read gold.sales.fact_sales.",
    );
    server.route({ [`GET ${TABLE_PATH}/definition`]: { status: 200, body: DEFINITION } });
    await user.click(within(fact).getByRole("button", { name: "Try again" }));

    expect(await definedIn()).toHaveTextContent("models/gold/fact_sales.sql, line 1");
  });

  it("opens the file in a dialog and marks the defining line", async () => {
    const { server, user } = renderShell(PAGE, {
      routes: { [`GET ${FILE_PATH}`]: { status: 200, body: definitionFile({ line: 2 }) } },
    });

    await definedIn();
    expect(server.sent.some((request) => request.path === FILE_PATH)).toBe(false);
    const dialog = await openFile(user);

    expect(dialog).toHaveAccessibleName("Where fact_sales is defined");
    const text = await fileText(dialog);
    expect(text).toHaveAttribute("tabindex", "0");
    expect(server.sent.filter((request) => request.path === FILE_PATH)).toHaveLength(1);
    const marked = within(text).getByText("grain: one row per sale", { exact: false });
    expect(marked.closest("mark")).not.toBeNull();
    expect(text.querySelectorAll("mark")).toHaveLength(1);
    expect(scrolled.mock.contexts).toEqual([marked.closest("mark")]);
    expect(text.textContent).toContain("SELECT * FROM silver.sales");
    expect(within(dialog).getByText("analytics")).toBeVisible();
    expect(
      within(dialog).getByText("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"),
    ).toBeInTheDocument();
  });

  it("renders_html_as_text", async () => {
    const content = "<img src=x onerror=alert(1)>\n<b>bold</b>";
    const { user } = renderShell(PAGE, {
      routes: { [`GET ${FILE_PATH}`]: { status: 200, body: definitionFile({ content }) } },
    });

    const text = await fileText(await openFile(user));

    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("b")).toBeNull();
    expect(text.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(text.textContent).toContain("<b>bold</b>");
  });

  it("marks_control_and_bidi_characters", async () => {
    const content = "\t a\u202Eb\u0007c\u2066d\u200Be\u0085f\u2028g\n";
    const { user } = renderShell(PAGE, {
      routes: { [`GET ${FILE_PATH}`]: { status: 200, body: definitionFile({ content }) } },
    });

    const text = await fileText(await openFile(user));

    const marks = [...text.querySelectorAll(".char-mark")].map((mark) => mark.textContent);
    expect(marks).toEqual(["U+202E", "U+0007", "U+2066", "U+200B", "U+0085", "U+2028"]);
    for (const mark of marks) {
      expect(within(text).getByText(mark)).toBeVisible();
    }
    const shown = text.textContent;
    expect(shown).not.toMatch(RAW);
    expect(shown).toContain("\t aU+202Eb");
    expect(shown).toContain("\n");
  });

  it("links to the git host only for an https link", async () => {
    const { user } = renderShell(PAGE);

    const fact = await definedIn();
    const link = within(fact).getByRole("link", { name: /Open in your git host/ });
    expect(link).toHaveAttribute("href", DEFINITION.browse_url);
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
    const dialog = await openFile(user);
    expect(within(dialog).getByRole("link", { name: /Open in your git host/ })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
  });

  it("shows no link for a link that is not https, nor for none", async () => {
    for (const browse of ["javascript:alert(1)", "http://github.com/a/b", null]) {
      renderShell(PAGE, {
        routes: {
          [`GET ${TABLE_PATH}/definition`]: {
            status: 200,
            body: { ...DEFINITION, browse_url: browse },
          },
        },
      });

      const fact = await definedIn();
      expect(fact).toHaveTextContent("models/gold/fact_sales.sql, line 1 · a1b2c3d");
      expect(within(fact).queryByRole("link")).not.toBeInTheDocument();
      cleanup();
    }
  });

  it("shows the server's sentence when the file is restricted", async () => {
    const message =
      "The file is shown only to people who may read this environment and every table the file uses, without a row filter or column mask. Open it in your git host if you have access there, or ask an administrator.";
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${FILE_PATH}`]: {
          status: 200,
          body: definitionFile({ content: null, bytes: null, reason: "restricted", message }),
        },
      },
    });

    const dialog = await openFile(user);

    expect(await within(dialog).findByText(message)).toBeVisible();
    expect(within(dialog).queryByRole("region")).not.toBeInTheDocument();
  });

  it("shows no text and says why when the read could not be recorded", async () => {
    const { server, user } = renderShell(PAGE, {
      routes: {
        [`GET ${FILE_PATH}`]: { status: 503, body: { detail: "This read was refused." } },
      },
    });

    const dialog = await openFile(user);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The file is not shown because reading it could not be recorded in the audit log. Try again in a moment; if it keeps failing, ask an administrator to check the server.",
    );
    expect(within(dialog).queryByRole("region")).not.toBeInTheDocument();
    server.route({ [`GET ${FILE_PATH}`]: { status: 200, body: definitionFile() } });
    await user.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(await fileText(dialog)).toHaveTextContent("SELECT * FROM silver.sales");
  });

  it("says so when no project defines the table any more by the time the file is read", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${FILE_PATH}`]: {
          status: 200,
          body: {
            defined: false,
            repository: null,
            path: null,
            commit: null,
            line: null,
            project_version_id: null,
            deployed_at: null,
            browse_url: null,
            content: null,
            bytes: null,
            reason: null,
            message: null,
          },
        },
      },
    });

    const dialog = await openFile(user);

    expect(
      await within(dialog).findByText(/No deployed project defines this table any more/),
    ).toBeVisible();
    expect(within(dialog).queryByRole("region")).not.toBeInTheDocument();
  });

  it("shows the server's sentence for any other refusal", async () => {
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${FILE_PATH}`]: { status: 404, body: { detail: "The table is gone." } },
      },
    });

    const dialog = await openFile(user);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The table is gone.");
  });

  it("marks invisible characters in the path too", async () => {
    const path = "models/gold/fact\u202Esales.sql";
    const { user } = renderShell(PAGE, {
      routes: {
        [`GET ${TABLE_PATH}/definition`]: { status: 200, body: { ...DEFINITION, path } },
        [`GET ${FILE_PATH}`]: {
          status: 200,
          body: definitionFile({ path, repository: "ana\u200Blytics" }),
        },
      },
    });

    const fact = await definedIn();
    expect(fact).toHaveTextContent("models/gold/factU+202Esales.sql, line 1");
    expect(fact.textContent).not.toMatch(RAW);
    const dialog = await openFile(user);
    await fileText(dialog);
    expect(dialog).toHaveTextContent("anaU+200Blytics");
    expect(dialog.textContent).not.toMatch(RAW);
  });

  it("moves focus back to the path after the dialog closes", async () => {
    const { user } = renderShell(PAGE);

    const dialog = await openFile(user);
    await fileText(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const path = within(await definedIn()).getByRole("button");
    await waitFor(() => {
      expect(path).toHaveFocus();
    });

    await openFile(user);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(path).toHaveFocus();
    });
  });
});
