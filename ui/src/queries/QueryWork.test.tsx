import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { forEachDiagnostic } from "@codemirror/lint";
import { EditorView } from "@codemirror/view";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { aPage, aRun, aSavedQuery, queryRoutes, RUN_ID, WAITING } from "../test/queries";
import { renderShell } from "../test/render";
import type { FakeRoute } from "../test/server";
import { EditorSkeleton, RunLine } from "./QueryWork";
import type { QueryRunState } from "./useQueryRun";

const RUN_PATH = `/queries/prod/runs/${RUN_ID}`;
const PROD_RUNS = "/api/v1/environments/prod/queries";
const STATUS = `${PROD_RUNS}/${RUN_ID}`;
const SAVED_ID = "6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d";
const SAVED_PATH = `/queries/prod/saved/${SAVED_ID}`;
const SAVED_ONE = `/api/v1/environments/prod/saved-queries/${SAVED_ID}`;
const RANKING = "select station_id from gold.station_ranking";

/**
 * The routes of the screen: the environments, the submit in `dev` and `prod`, the run's status
 * (succeeded), its first page and the record of an opened query.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function routes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...queryRoutes(),
    [`POST ${PROD_RUNS}`]: { status: 202, body: { query_id: RUN_ID } },
    "POST /api/v1/environments/dev/queries": { status: 202, body: { query_id: RUN_ID } },
    "GET /api/v1/environments/dev/queries/0b1c2d3e-4f50-4a61-b072-8394a5b6c7d8": {
      status: 200,
      body: aRun(),
    },
    "GET /api/v1/environments/dev/queries/0b1c2d3e-4f50-4a61-b072-8394a5b6c7d8/result": {
      status: 200,
      body: aPage(),
    },
    [`GET ${STATUS}/result`]: { status: 200, body: aPage() },
    "POST /api/v1/principals/admin/recent": { status: 204 },
    ...more,
  };
}

/**
 * Waits for the SQL editor and hands back its view.
 *
 * @returns The editor's view.
 */
async function editor(): Promise<EditorView> {
  const shadow = await waitFor(() => {
    const found = document.querySelector(".sql-editor")?.shadowRoot ?? null;
    expect(found).not.toBeNull();
    return found as ShadowRoot;
  });
  const box = within(shadow as unknown as HTMLElement).getByRole("textbox", { name: "SQL" });
  return EditorView.findFromDOM(box) as EditorView;
}

/**
 * Replaces the editor's text, as typing does.
 *
 * @param view - The editor's view.
 * @param text - The new text.
 */
function type(view: EditorView, text: string): void {
  act(() => {
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
  });
}

/**
 * The requests sent, as `METHOD path`.
 *
 * @param shell - The rendered shell.
 * @returns The requests.
 */
function sent(shell: ReturnType<typeof renderShell>): string[] {
  return shell.server.sent.map((request) => `${request.method} ${request.path}`);
}

describe("the Queries screen's work", () => {
  it("starts on the remembered environment and remembers a new choice", async () => {
    window.localStorage.setItem("lakana.queries.environment", "prod");
    const { user } = renderShell("/queries", { routes: routes() });

    await user.click(await screen.findByRole("button", { name: "Environment: prod, protected" }));
    await user.click(screen.getByRole("option", { name: "dev" }));

    expect(screen.getByRole("button", { name: "Environment: dev" })).toBeInTheDocument();
    expect(window.localStorage.getItem("lakana.queries.environment")).toBe("dev");
  });

  it("falls back to the first listed environment when the remembered one is gone", async () => {
    window.localStorage.setItem("lakana.queries.environment", "staging");
    const shell = renderShell("/queries", { routes: routes() });

    expect(await screen.findByRole("button", { name: "Environment: dev" })).toBeInTheDocument();
    type(await editor(), "select 1 as one");
    await shell.user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByRole("grid");

    expect(sent(shell)).toContain("POST /api/v1/environments/dev/queries");
    expect(sent(shell).filter((request) => request.includes("staging"))).toEqual([]);
    expect(window.localStorage.getItem("lakana.queries.environment")).toBe("dev");
  });

  it("says how to get the right when no environment is listed", async () => {
    renderShell("/queries", {
      routes: routes({ "GET /api/v1/queries/environments": { status: 200, body: [] } }),
    });

    expect(
      await screen.findByText(
        "You may not run queries in any environment yet. Ask an administrator for the right to run queries (SUBMIT_QUERY).",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('lakana sql "select 1" --env <environment>')).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
    expect(document.querySelector(".sql-editor")).toBeNull();
  });

  it("says why the environments could not be read", async () => {
    renderShell("/queries", {
      routes: routes({
        "GET /api/v1/queries/environments": {
          status: 500,
          body: { detail: "Something broke on the server.", type: "InternalError" },
        },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Something broke on the server.");
  });

  it("Run submits the editor's SQL in the chosen environment and moves to the run's address", async () => {
    window.localStorage.setItem("lakana.queries.environment", "prod");
    const shell = renderShell("/queries", { routes: routes() });
    type(await editor(), "select 1 as one");
    const run = screen.getByRole("button", { name: "Run" });
    expect(run).toHaveClass("button-primary");
    expect(run).toHaveTextContent(/Run(Ctrl ↵|⌘↵)$/);

    await shell.user.click(run);

    await waitFor(() => {
      expect(shell.router.state.location.pathname).toBe(RUN_PATH);
    });
    const submit = shell.server.sent.find(
      (request) => request.method === "POST" && request.path === PROD_RUNS,
    );
    expect(submit?.body).toEqual({ sql: "select 1 as one" });
    expect(
      await screen.findByRole("grid", { name: "Result of Query 0b1c2d3e, rows" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Query 0b1c2d3e")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toHaveFocus();
    expect((await editor()).state.doc.toString()).toBe("select 1 as one");
  });

  it("runs with Ctrl Enter in the editor, once while the submit is answered", async () => {
    let answer: () => void = () => undefined;
    const shell = renderShell("/queries", {
      routes: routes({
        "POST /api/v1/environments/dev/queries": () =>
          new Promise((resolve) => {
            answer = () => {
              resolve({ status: 202, body: { query_id: RUN_ID } });
            };
          }),
      }),
    });
    const view = await editor();
    type(view, "select 1 as one");
    const press = (): void => {
      act(() => {
        view.contentDOM.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
        );
      });
    };

    press();
    await waitFor(() => {
      expect(sent(shell)).toContain("POST /api/v1/environments/dev/queries");
    });
    press();
    answer();

    expect(await screen.findByRole("grid")).toBeInTheDocument();
    expect(
      sent(shell).filter((request) => request === "POST /api/v1/environments/dev/queries"),
    ).toHaveLength(1);
  });

  it("Run becomes Cancel while the run is active and Cancel returns the focus to Run", async () => {
    let cancelled = false;
    const shell = renderShell("/queries", {
      routes: routes({
        [`GET ${STATUS}`]: () => ({
          status: 200,
          body: aRun(cancelled ? { status: "CANCELLED" } : { status: "RUNNING" }),
        }),
        [`POST ${STATUS}/cancel`]: () => {
          cancelled = true;
          return { status: 200, body: aRun({ status: "CANCELLED" }) };
        },
      }),
    });
    await shell.user.click(await screen.findByRole("button", { name: /^Environment/ }));
    await shell.user.click(screen.getByRole("option", { name: /prod/ }));
    const view = await editor();
    type(view, "select 1 as one");

    await shell.user.click(screen.getByRole("button", { name: "Run" }));
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    expect(screen.getByText("Running")).toBeInTheDocument();
    act(() => {
      view.contentDOM.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
    });
    expect(sent(shell).filter((request) => request === `POST ${PROD_RUNS}`)).toHaveLength(1);
    await shell.user.click(cancel);

    expect(await screen.findByText("Cancelling…")).toBeInTheDocument();
    const run = await screen.findByRole("button", { name: "Run" }, { timeout: 3_000 });
    expect(run).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent("The query was cancelled.");
    expect(sent(shell).filter((request) => request.endsWith("/cancel"))).toHaveLength(1);
  });

  it("does not cancel the run when the page is left and shows it again from its address", async () => {
    let done = false;
    const shell = renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}`]: () => ({
          status: 200,
          body: aRun(done ? {} : { status: "RUNNING", finished_at: null, result_rows: null }),
        }),
      }),
    });
    expect(await screen.findByRole("button", { name: "Cancel" })).toBeInTheDocument();

    await act(() => shell.router.navigate("/catalog"));
    await screen.findByRole("heading", { level: 1, name: "Catalog" });
    await act(() => shell.router.navigate(RUN_PATH));
    expect(await screen.findByRole("button", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();

    done = true;

    expect(await screen.findByRole("grid", {}, { timeout: 3_000 })).toBeInTheDocument();
    expect(sent(shell).filter((request) => request.endsWith("/cancel"))).toEqual([]);
  });

  it("shows a refusal through problemText above the result", async () => {
    const shell = renderShell(RUN_PATH, {
      routes: routes({
        [`POST ${PROD_RUNS}`]: {
          status: 422,
          body: { detail: "Only one statement may run at a time.", type: "QueryRefused" },
        },
      }),
    });
    await screen.findByRole("grid");
    type(await editor(), "select 1; select 2");

    await shell.user.click(screen.getByRole("button", { name: "Run" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Only one statement may run at a time.");
    expect(shell.router.state.location.pathname).toBe(RUN_PATH);
  });

  it("shows a failed run's message and marks its line and column, and Go to puts the cursor there", async () => {
    const shell = renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}`]: {
          status: 200,
          body: aRun({
            status: "FAILED",
            sql: "select\n  nope from t",
            error_message: "Column nope does not exist.",
            error_cause: "invalid_query",
            error_line: 2,
            error_column: 3,
          }),
        },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Column nope does not exist.");
    const view = await editor();
    await waitFor(() => {
      expect(view.dom.querySelector(".cm-lintRange-error")).not.toBeNull();
    });
    await shell.user.click(screen.getByRole("button", { name: "Go to line 2, column 3" }));

    expect(view.state.selection.main.head).toBe("select\n  ".length);
    expect(view.hasFocus).toBe(true);
  });

  it("marks a hidden character of the server's message on the editor's error mark", async () => {
    const message = `Column ${String.fromCodePoint(0x202e)}nope does not exist.`;
    renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}`]: {
          status: 200,
          body: aRun({ status: "FAILED", error_message: message, error_line: 1, error_column: 1 }),
        },
      }),
    });
    await screen.findByRole("alert");
    const view = await editor();

    const messages: string[] = [];
    await waitFor(() => {
      messages.length = 0;
      forEachDiagnostic(view.state, (diagnostic) => {
        messages.push(diagnostic.message);
      });
      expect(messages).toEqual(["Column U+202Enope does not exist."]);
    });
  });

  it("says why a refused cancel did not cancel, and Cancel can be pressed again", async () => {
    const shell = renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}`]: {
          status: 200,
          body: aRun({ status: "RUNNING", finished_at: null, result_rows: null }),
        },
        [`POST ${STATUS}/cancel`]: {
          status: 503,
          body: { detail: "The server is busy; try again.", type: "Busy" },
        },
      }),
    });

    await shell.user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The query was not cancelled and runs on: The server is busy; try again. Press Cancel to try again.",
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("opening a recent run loads its SQL and environment, its state and its first page", async () => {
    window.localStorage.setItem("lakana.queries.environment", "dev");
    renderShell(RUN_PATH, { routes: routes() });

    expect(
      await screen.findByRole("grid", { name: "Result of Query 0b1c2d3e, rows" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Environment: prod, protected" }),
    ).toBeInTheDocument();
    expect((await editor()).state.doc.toString()).toBe("select 1 as one");
    expect(screen.getByText("1 row · 1.0 s")).toHaveAttribute("role", "status");
    expect(window.localStorage.getItem("lakana.queries.environment")).toBe("dev");
  });

  it("an opened run whose SQL the server does not keep leaves the editor as it was", async () => {
    renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}`]: { status: 200, body: aRun({ origin: "preview", sql: null }) },
      }),
    });

    expect(await screen.findByRole("grid")).toBeInTheDocument();
    expect((await editor()).state.doc.toString()).toBe("");
  });

  it("a run whose result is no longer kept says so", async () => {
    renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}/result`]: {
          status: 409,
          body: { detail: "The query has no stored result.", type: "ConflictError" },
        },
      }),
    });

    expect(await screen.findByText("This run's result is no longer kept.")).toBeInTheDocument();
  });

  it("records the opened query for Home", async () => {
    const shell = renderShell(RUN_PATH, { routes: routes() });
    await screen.findByRole("grid");

    const record = shell.server.sent.filter(
      (request) => request.method === "POST" && request.path === "/api/v1/principals/admin/recent",
    );
    expect(record.map((request) => request.body)).toEqual([
      { kind: "query", environment: "prod", ref: RUN_ID },
    ]);
  });

  it("Try again follows a run whose state could not be read", async () => {
    let failing = true;
    const shell = renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}/result`]: () =>
          failing
            ? { status: 500, body: { detail: "Something broke.", type: "InternalError" } }
            : { status: 200, body: aPage() },
      }),
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Something broke.");

    failing = false;
    await shell.user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("grid")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toHaveFocus();
  });

  it("Show 100 more appends the next page of the result", async () => {
    const shell = renderShell(RUN_PATH, {
      routes: routes({
        [`GET ${STATUS}/result`]: (request) => ({
          status: 200,
          body: request.path.endsWith("offset=0")
            ? aPage({ row_count: 2, next_offset: 1 })
            : aPage({ rows: [[2]], row_count: 2 }),
        }),
      }),
    });
    await screen.findByRole("grid");

    await shell.user.click(screen.getByRole("button", { name: "Show 100 more" }));

    await waitFor(() => {
      expect(within(screen.getByRole("grid")).getAllByRole("row")).toHaveLength(3);
    });
  });

  it("an unknown address under Queries is not found", async () => {
    renderShell("/queries/prod/elsewhere", { routes: routes() });

    expect(
      await screen.findByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeInTheDocument();
  });

  it("Format replaces the text and keeps the run state", async () => {
    const shell = renderShell(RUN_PATH, { routes: routes() });
    expect(await screen.findByRole("grid")).toBeInTheDocument();
    const view = await editor();
    type(view, "select station_id from gold.station_ranking");
    const before = sent(shell).length;

    await shell.user.click(screen.getByRole("button", { name: "Format" }));

    await waitFor(() => {
      expect(view.state.doc.toString()).toBe("SELECT\n  station_id\nFROM\n  gold.station_ranking");
    });
    expect(
      screen.getByRole("grid", { name: "Result of Query 0b1c2d3e, rows" }),
    ).toBeInTheDocument();
    expect(screen.getByText("1 row · 1.0 s")).toBeInTheDocument();
    expect(shell.router.state.location.pathname).toBe(RUN_PATH);
    expect(sent(shell)).toHaveLength(before);
    expect(screen.queryByText(/Format could not read/)).toBeNull();

    // Run sends the formatted text.
    await shell.user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => {
      expect(
        shell.server.sent.find((request) => request.method === "POST" && request.path === PROD_RUNS)
          ?.body,
      ).toEqual({ sql: "SELECT\n  station_id\nFROM\n  gold.station_ranking" });
    });
  });

  it("Format of unreadable SQL says so and leaves the text", async () => {
    const shell = renderShell("/queries", { routes: routes() });
    const view = await editor();
    type(view, "select 'unclosed from gold.station_ranking");

    await shell.user.click(screen.getByRole("button", { name: "Format" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Format could not read this SQL; run it to see the server's reason.",
    );
    expect(view.state.doc.toString()).toBe("select 'unclosed from gold.station_ranking");

    // The next edit takes the line away.
    act(() => {
      view.dispatch({ changes: { from: view.state.doc.length, insert: "'" } });
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("the editor fills its resizable host, so a click below short SQL lands in it", () => {
    const css = readFileSync(resolve(import.meta.dirname, "queries.css"), "utf8");
    expect(css).toMatch(/\.queries-editor\s*{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;/);
    expect(css).toMatch(/\.sql-editor\s*{[^}]*display:\s*flex;[^}]*flex:\s*1 1 auto;/);
    const theme = readFileSync(resolve(import.meta.dirname, "editor/theme.ts"), "utf8");
    expect(theme).toMatch(/"&":\s*{[^}]*flex:\s*"1 1 auto"/);
  });

  it("the toolbar and the result wrap at 320 px", async () => {
    renderShell(RUN_PATH, { routes: routes() });
    await screen.findByRole("grid");

    expect(document.querySelector(".queries .queries-work .queries-toolbar")).not.toBeNull();
    expect(document.querySelector(".queries-result .queries-result-bar")).not.toBeNull();
    // Vitest hands CSS imports back empty, so the stylesheet is read directly.
    const css = readFileSync(resolve(import.meta.dirname, "queries.css"), "utf8");
    expect(css).toMatch(/\.queries-toolbar\s*{[^}]*flex-wrap:\s*wrap;/);
    expect(css).toMatch(/\.queries-result-bar\s*{[^}]*flex-wrap:\s*wrap;/);
    expect(css).toMatch(/\.queries-work\s*{[^}]*min-width:\s*0;/);
  });

  it("opening a saved query loads its SQL and environment", async () => {
    window.localStorage.setItem("lakana.queries.environment", "dev");
    const shell = renderShell(SAVED_PATH, {
      routes: routes({
        [`GET ${SAVED_ONE}`]: { status: 200, body: aSavedQuery({ sql: RANKING }) },
      }),
    });

    expect(
      await screen.findByRole("button", { name: "Environment: prod, protected" }),
    ).toBeVisible();
    await waitFor(async () => {
      expect((await editor()).state.doc.toString()).toBe(RANKING);
    });
    expect(document.querySelector(".queries-name")).toHaveTextContent(/^Daily sales$/);
    expect(screen.getByRole("button", { name: "Save…" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete…" })).toBeInTheDocument();
    expect(screen.queryByRole("grid")).toBeNull();

    await shell.user.click(screen.getByRole("button", { name: "Run" }));

    expect(
      await screen.findByRole("grid", { name: "Result of Daily sales, rows" }),
    ).toBeInTheDocument();
    expect(shell.router.state.location.pathname).toBe(RUN_PATH);
    expect(
      shell.server.sent.find((request) => request.method === "POST" && request.path === PROD_RUNS)
        ?.body,
    ).toEqual({ sql: RANKING });
  });

  it("an administrator's view of a private query says the SQL is not readable, disables Run and offers Delete", async () => {
    // As the server answers an administrator: no `sql` at all.
    const unreadable = Object.fromEntries(
      Object.entries(aSavedQuery({ actions: ["delete"] })).filter(([key]) => key !== "sql"),
    );
    const shell = renderShell(SAVED_PATH, {
      routes: routes({ [`GET ${SAVED_ONE}`]: { status: 200, body: unreadable } }),
    });

    expect(await screen.findByText("You may not read the SQL of this query.")).toBeInTheDocument();
    const facts = document.querySelector(".queries-saved-facts");
    expect(facts).toHaveTextContent("by kim.lee");
    expect(facts).toHaveTextContent("Environment prod");
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Format" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete…" })).toBeEnabled();
    const view = await editor();
    expect(view.state.doc.toString()).toBe("");
    expect(view.state.readOnly).toBe(true);

    // Ctrl Enter in the read-only editor runs nothing either.
    act(() => {
      view.contentDOM.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
    });
    expect(sent(shell).filter((request) => request.startsWith("POST"))).toEqual([]);
  });

  it("after a delete the note is focused and the SQL stays", async () => {
    const shell = renderShell(SAVED_PATH, {
      routes: routes({ [`DELETE ${SAVED_ONE}`]: { status: 204 } }),
    });
    const view = await editor();
    await waitFor(() => {
      expect(view.state.doc.toString()).toBe("select 1 as one");
    });
    type(view, "select 2 as two");
    const reads = (): number =>
      sent(shell).filter((request) => request.startsWith("GET /api/v1/saved-queries?")).length;
    await waitFor(() => {
      expect(reads()).toBe(1);
    });

    await shell.user.click(screen.getByRole("button", { name: "Delete…" }));
    const dialog = screen.getByRole("dialog", { name: "Delete the saved query Daily sales?" });
    await shell.user.click(within(dialog).getByRole("button", { name: "Delete" }));

    const note = await screen.findByText("Deleted Daily sales.");
    await waitFor(() => {
      expect(note).toHaveFocus();
    });
    expect(note).toHaveAttribute("role", "status");
    expect(shell.router.state.location.pathname).toBe("/queries");
    expect((await editor()).state.doc.toString()).toBe("select 2 as two");
    expect(screen.queryByRole("button", { name: "Delete…" })).toBeNull();
    expect(screen.getByText("Untitled query")).toBeInTheDocument();
    await waitFor(() => {
      expect(reads()).toBe(2);
    });
  });

  it("after save as the address is the saved query's and the Saved list shows it", async () => {
    const created = aSavedQuery({ id: "7b8c9d0e", environment: "dev", name: "Late trains" });
    let listed = [aSavedQuery()];
    const shell = renderShell("/queries", {
      routes: routes({
        "GET /api/v1/saved-queries": () => ({
          status: 200,
          body: { items: listed, next_cursor: null },
        }),
        "POST /api/v1/environments/dev/saved-queries": () => {
          listed = [aSavedQuery(), created];
          return { status: 201, body: created };
        },
        "GET /api/v1/environments/dev/saved-queries/7b8c9d0e": { status: 200, body: created },
      }),
    });
    type(await editor(), "select 1 as one");
    const save = screen.getByRole("button", { name: "Save…" });

    await shell.user.click(save);
    const dialog = screen.getByRole("dialog", { name: "Save the query" });
    await shell.user.type(within(dialog).getByRole("textbox", { name: "Name" }), "Late trains");
    await shell.user.click(within(dialog).getByRole("button", { name: "Save as new" }));

    await waitFor(() => {
      expect(shell.router.state.location.pathname).toBe("/queries/dev/saved/7b8c9d0e");
    });
    const saved = await screen.findByRole("region", { name: "Saved queries" });
    expect(await within(saved).findByRole("link", { name: "Late trains" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(save).toHaveFocus();
    });
    expect(document.querySelector(".queries-name")).toHaveTextContent(/^Late trains$/);
  });

  it("Save after a run of a saved query updates it and keeps the run's address", async () => {
    const updated = aSavedQuery({ name: "Daily sales by region" });
    const shell = renderShell(SAVED_PATH, {
      routes: routes({ [`PUT ${SAVED_ONE}`]: { status: 200, body: updated } }),
    });
    const view = await editor();
    await waitFor(() => {
      expect(view.state.doc.toString()).toBe("select 1 as one");
    });
    await shell.user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByRole("grid", { name: "Result of Daily sales, rows" });

    await shell.user.click(screen.getByRole("button", { name: "Save…" }));
    const dialog = screen.getByRole("dialog", { name: "Save the query" });
    await shell.user.type(within(dialog).getByRole("textbox", { name: "Name" }), " by region");
    await shell.user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(document.querySelector(".queries-name")).toHaveTextContent(/^Daily sales by region$/);
    });
    expect(shell.router.state.location.pathname).toBe(RUN_PATH);
    expect(screen.getByRole("grid")).toBeInTheDocument();
    expect(sent(shell)).toContain(`PUT ${SAVED_ONE}`);
  });

  it("a saved query in dev with the picker on prod is saved as new in prod, never replaced", async () => {
    const inDev = aSavedQuery({ environment: "dev" });
    const created = aSavedQuery({ id: "8c9d0e1f", name: "Daily sales" });
    const shell = renderShell(`/queries/dev/saved/${SAVED_ID}`, {
      routes: routes({
        [`GET /api/v1/environments/dev/saved-queries/${SAVED_ID}`]: { status: 200, body: inDev },
        "POST /api/v1/environments/prod/saved-queries": { status: 201, body: created },
        "GET /api/v1/environments/prod/saved-queries/8c9d0e1f": { status: 200, body: created },
      }),
    });
    await waitFor(() => {
      expect(document.querySelector(".queries-name")).toHaveTextContent(/^Daily sales$/);
    });
    await shell.user.click(screen.getByRole("button", { name: "Environment: dev" }));
    await shell.user.click(screen.getByRole("option", { name: /^prod/ }));

    await shell.user.click(screen.getByRole("button", { name: "Save…" }));
    const dialog = screen.getByRole("dialog", { name: "Save the query" });
    expect(within(dialog).queryByRole("button", { name: "Save" })).toBeNull();
    expect(dialog).toHaveTextContent("It is saved in prod.");
    await shell.user.click(within(dialog).getByRole("button", { name: "Save as new" }));

    await waitFor(() => {
      expect(shell.router.state.location.pathname).toBe("/queries/prod/saved/8c9d0e1f");
    });
    expect(sent(shell)).toContain("POST /api/v1/environments/prod/saved-queries");
    expect(sent(shell).filter((request) => request.startsWith("PUT"))).toEqual([]);
  });

  it("names no query while a saved query's address is read", async () => {
    let answer: () => void = () => undefined;
    renderShell(SAVED_PATH, {
      routes: routes({
        [`GET ${SAVED_ONE}`]: () =>
          new Promise((resolve) => {
            answer = () => {
              resolve({ status: 200, body: aSavedQuery() });
            };
          }),
      }),
    });
    await screen.findByRole("button", { name: "Save…" });

    expect(document.querySelector(".queries-name")).toHaveTextContent(/^$/);
    expect(screen.queryByText("Untitled query")).toBeNull();
    answer();

    await waitFor(() => {
      expect(document.querySelector(".queries-name")).toHaveTextContent(/^Daily sales$/);
    });
  });

  it("a saved query that cannot be read says why", async () => {
    renderShell(SAVED_PATH, {
      routes: routes({
        [`GET ${SAVED_ONE}`]: {
          status: 404,
          body: { type: "NotFoundError", detail: "There is no saved query with this id." },
        },
      }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "There is no saved query with this id.",
    );
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  });

  it("back at a new query no run shows, and the remembered environment is read again", async () => {
    const shell = renderShell(RUN_PATH, { routes: routes() });
    await screen.findByRole("grid");
    window.localStorage.setItem("lakana.queries.environment", "dev");

    await act(async () => {
      await shell.router.navigate("/queries");
    });

    expect(await screen.findByRole("button", { name: "Environment: dev" })).toBeInTheDocument();
    expect(screen.queryByRole("grid")).toBeNull();
    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(screen.getByText("Untitled query")).toBeInTheDocument();
  });
});

describe("RunLine", () => {
  const IDLE: QueryRunState = { phase: "idle", run: null, page: null, error: null };

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    ["submitting", { ...IDLE, phase: "submitting" }, "Submitting the query…"],
    ["starting", { ...IDLE, phase: "waiting" }, "Starting the query…"],
    ["running", { ...IDLE, phase: "waiting", run: aRun({ status: "RUNNING" }) }, "Running"],
    ["reading", { ...IDLE, phase: "loading_page", run: aRun() }, "Reading the rows…"],
    ["failed", { ...IDLE, phase: "done", run: aRun({ status: "FAILED" }) }, "Failed"],
    ["idle", IDLE, ""],
    ["lost", { ...IDLE, phase: "failed" }, ""],
    ["refused", { ...IDLE, phase: "refused" }, ""],
  ] as const)("says the state while %s", (_, state, words) => {
    render(<RunLine state={state} />);

    expect(screen.getByRole("status")).toHaveTextContent(new RegExp(`^${words}$`));
  });

  it("says why a queued run waits after a second", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    render(
      <RunLine
        state={{ ...IDLE, phase: "waiting", run: aRun({ status: "QUEUED", waiting_for: WAITING }) }}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/^Queued$/);

    act(() => {
      vi.advanceTimersByTime(1_000);
    });

    expect(screen.getByRole("status")).toHaveTextContent(`Queued${WAITING.message}`);
  });

  it("a queued run without a reason says only its state", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    render(<RunLine state={{ ...IDLE, phase: "waiting", run: aRun({ status: "QUEUED" }) }} />);

    act(() => {
      vi.advanceTimersByTime(1_000);
    });

    expect(screen.getByRole("status")).toHaveTextContent(/^Queued$/);
  });
});

describe("EditorSkeleton", () => {
  it("says it is loading the editor after a second", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const { container } = render(<EditorSkeleton />);
    const skeleton = container.querySelector(".queries-editor-skeleton");
    expect(skeleton).toHaveTextContent(/^$/);

    act(() => {
      vi.advanceTimersByTime(1_000);
    });

    expect(skeleton).toHaveTextContent("Loading the editor…");
    vi.useRealTimers();
  });
});
