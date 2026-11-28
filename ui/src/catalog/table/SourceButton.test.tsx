import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type FakeRoute, fakeServer } from "../../test/server";
import { definitionFile, TABLE_PATH } from "../../test/table";
import { SourceButton } from "./SourceButton";

const FILE_PATH = `${TABLE_PATH}/definition/file`;

/** Every character the display rule marks, as a pattern; written with escapes only. */
// Matching control characters is the point of this pattern.
// eslint-disable-next-line no-control-regex
const RAW = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u2028\u2029\p{Cf}]/u;

/** What differs from the plain button. */
interface ButtonOptions {
  /** What the server answers, by `METHOD path`. */
  readonly routes?: Record<string, FakeRoute>;
  /** The fields of the table that differ. */
  readonly place?: { name?: string };
  /** The button's label. */
  readonly label?: string;
}

/**
 * Renders the button for the fact table, the server answering `routes`.
 *
 * @param options - What differs.
 * @param options.routes - What the server answers.
 * @param options.place - The fields of the table that differ.
 * @param options.label - The button's label.
 * @returns The fake server and the user-event session.
 */
function renderButton({ routes = {}, place = {}, label = "Show source" }: ButtonOptions = {}) {
  const server = fakeServer({
    [`GET ${FILE_PATH}`]: { status: 200, body: definitionFile() },
    ...routes,
  });
  render(
    <SourceButton
      place={{ environment: "prod", namespace: "gold.sales", name: "fact_sales", ...place }}
      label={label}
    />,
  );
  return { server, user: userEvent.setup() };
}

/**
 * Opens the file's dialog with the button.
 *
 * @param user - The user-event session.
 * @returns The dialog.
 */
async function openFile(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await user.click(screen.getByRole("button"));
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

describe("SourceButton", () => {
  // jsdom lays nothing out and has no scrollIntoView; the spy records what was scrolled to.
  const scrolled = vi.fn();
  beforeEach(() => {
    scrolled.mockClear();
    Element.prototype.scrollIntoView = scrolled;
  });
  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  });

  it("marks a hidden character of the table's name in the dialog's heading and name", async () => {
    const { user } = renderButton({ place: { name: "fact_\u202Esales" } });

    const dialog = await openFile(user);

    expect(dialog).toHaveAccessibleName("Where fact_U+202Esales is defined");
    const heading = within(dialog).getByRole("heading", { level: 2 });
    expect(heading).toHaveTextContent("Where fact_U+202Esales is defined");
    expect(heading.textContent).not.toMatch(RAW);
  });

  it("opens with the label given", async () => {
    const { server, user } = renderButton({ label: "Show source" });

    expect(screen.getByRole("button")).toHaveTextContent("Show source");
    expect(server.sent).toHaveLength(0);
    const dialog = await openFile(user);
    expect(await fileText(dialog)).toBeInTheDocument();
  });

  it("opens the file in a dialog and marks the defining line", async () => {
    const { server, user } = renderButton({
      routes: { [`GET ${FILE_PATH}`]: { status: 200, body: definitionFile({ line: 2 }) } },
    });

    await screen.findByRole("button");
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
    const { user } = renderButton({
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
    const { user } = renderButton({
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

  it("marks a hidden character in the server's sentence about the file", async () => {
    const { user } = renderButton({
      routes: {
        [`GET ${FILE_PATH}`]: {
          status: 200,
          body: definitionFile({
            content: null,
            bytes: null,
            reason: "restricted",
            message: "Ask about analy\u202Etics.",
          }),
        },
      },
    });

    const dialog = await openFile(user);

    const mark = await within(dialog).findByText("U+202E", { selector: "p > .char-mark" });
    expect(mark.parentElement).toHaveTextContent("Ask about analyU+202Etics.");
    expect(dialog.textContent).not.toMatch(RAW);
  });

  it("shows neither text nor sentence when the server sends no content and no reason", async () => {
    const { user } = renderButton({
      routes: {
        [`GET ${FILE_PATH}`]: {
          status: 200,
          body: definitionFile({ content: null, bytes: null, reason: null, message: null }),
        },
      },
    });

    const dialog = await openFile(user);

    await within(dialog).findByText("analytics");
    expect(within(dialog).queryByRole("region")).not.toBeInTheDocument();
  });

  it("shows the server's sentence when the file is restricted", async () => {
    const message =
      "The file is shown only to people who may read this environment and every table the file uses, without a row filter or column mask. Open it in your git host if you have access there, or ask an administrator.";
    const { user } = renderButton({
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
    const { server, user } = renderButton({
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
    const { user } = renderButton({
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
    const { user } = renderButton({
      routes: {
        [`GET ${FILE_PATH}`]: { status: 404, body: { detail: "The table is gone." } },
      },
    });

    const dialog = await openFile(user);

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The table is gone.");
  });

  it("moves focus back to the path after the dialog closes", async () => {
    const { user } = renderButton();

    const dialog = await openFile(user);
    await fileText(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const path = screen.getByRole("button");
    await waitFor(() => {
      expect(path).toHaveFocus();
    });

    await user.click(within(await openFile(user)).getByRole("button", { name: "Close dialog" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
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
