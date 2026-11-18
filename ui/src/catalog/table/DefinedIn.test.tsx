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
  // jsdom has no scrollIntoView, which the file's dialog calls.
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
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
});
