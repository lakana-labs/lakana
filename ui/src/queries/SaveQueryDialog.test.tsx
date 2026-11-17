import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SavedQueryOut } from "../api/savedQueries";
import { expectPending, held } from "../test/pending";
import { aSavedQuery } from "../test/queries";
import { fakeServer } from "../test/server";
import { SaveQueryDialog } from "./SaveQueryDialog";

const CREATE = "POST /api/v1/environments/dev/saved-queries";
const OPENED = aSavedQuery({ environment: "dev" });
const UPDATE = `PUT /api/v1/environments/dev/saved-queries/${OPENED.id}`;

/**
 * Renders the dialog for the SQL `select 2`, picked in `dev`.
 *
 * @param opened - The saved query open in the editor, or null.
 * @returns The handlers and a user-event session.
 */
function renderSave(opened: SavedQueryOut | null = null) {
  const onSaved = vi.fn();
  const onCancel = vi.fn();
  const user = userEvent.setup();
  render(
    <SaveQueryDialog
      environment="dev"
      sql="select 2"
      opened={opened}
      onSaved={onSaved}
      onCancel={onCancel}
    />,
  );
  return { onSaved, onCancel, user };
}

/**
 * The requests that change something, as `METHOD path`.
 *
 * @param sent - Every request.
 * @returns The writes.
 */
function writes(sent: readonly { method: string; path: string }[]): string[] {
  return sent
    .filter((request) => request.method !== "GET")
    .map((request) => `${request.method} ${request.path}`);
}

describe("SaveQueryDialog", () => {
  it("save as new sends name, sql, visibility and the picker's environment", async () => {
    const created = aSavedQuery({ id: "n1", environment: "dev", name: "Late trains" });
    const server = fakeServer({ [CREATE]: { status: 201, body: created } });
    const { onSaved, user } = renderSave();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Save the query");
    expect(screen.getByRole("dialog")).toHaveTextContent("It is saved in dev.");
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    const create = screen.getByRole("button", { name: "Save as new" });
    expect(create).toBeDisabled();

    await user.type(screen.getByRole("textbox", { name: "Name" }), "  Late trains ");
    await user.click(screen.getByRole("radio", { name: "Everyone who can run queries in dev" }));
    await user.click(create);

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(created, true);
    });
    expect(writes(server.sent)).toEqual([CREATE]);
    expect(server.sent[0]?.body).toEqual({
      name: "Late trains",
      sql: "select 2",
      visibility: "everyone",
    });
  });

  it("Save updates only an own opened query", async () => {
    const updated = aSavedQuery({
      environment: "dev",
      name: "Daily sales by region",
      sql: "select 2",
    });
    const server = fakeServer({ [UPDATE]: { status: 200, body: updated } });
    const { onSaved, user } = renderSave(OPENED);
    const name = screen.getByRole("textbox", { name: "Name" });
    expect(name).toHaveValue("Daily sales");
    expect(screen.getByRole("radio", { name: "Only me" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Save" })).toHaveClass("button-primary");
    expect(screen.getByRole("button", { name: "Save as new" })).not.toHaveClass("button-primary");

    await user.type(name, " by region{Enter}");

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(updated, false);
    });
    expect(writes(server.sent)).toEqual([UPDATE]);
    expect(server.sent[0]?.body).toEqual({
      name: "Daily sales by region",
      sql: "select 2",
      visibility: "private",
    });
  });

  it("offers only Save as new for a query the person does not own, and saves it as theirs", async () => {
    const created = aSavedQuery({ id: "n2", environment: "dev" });
    const server = fakeServer({ [CREATE]: { status: 201, body: created } });
    const { onSaved, user } = renderSave(
      aSavedQuery({ owner: "sam.ortiz", visibility: "everyone", actions: [] }),
    );
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: "Everyone who can run queries in dev" }),
    ).toBeChecked();

    await user.click(screen.getByRole("button", { name: "Save as new" }));

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(created, true);
    });
    expect(writes(server.sent)).toEqual([CREATE]);
  });

  it("offers only Save as new when the picker names another environment than the opened query's", async () => {
    const created = aSavedQuery({ id: "n4", environment: "dev" });
    const server = fakeServer({ [CREATE]: { status: 201, body: created } });
    const { onSaved, user } = renderSave(aSavedQuery({ environment: "prod" }));
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent("It is saved in dev.");

    await user.click(screen.getByRole("button", { name: "Save as new" }));

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(created, true);
    });
    expect(writes(server.sent)).toEqual([CREATE]);
  });

  it("an own opened query can also be saved as new", async () => {
    const created = aSavedQuery({ id: "n3", environment: "dev" });
    const server = fakeServer({ [CREATE]: { status: 201, body: created } });
    const { onSaved, user } = renderSave(OPENED);

    await user.click(screen.getByRole("button", { name: "Save as new" }));

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledWith(created, true);
    });
    expect(writes(server.sent)).toEqual([CREATE]);
  });

  it("only the pressed button shows that it waits", async () => {
    const answer = held();
    fakeServer({ [CREATE]: answer.route, [UPDATE]: answer.route });
    const { onSaved, user } = renderSave(OPENED);
    const save = screen.getByRole("button", { name: "Save" });
    const create = screen.getByRole("button", { name: "Save as new" });

    await user.click(create);

    expectPending(create);
    expect(save).not.toHaveAttribute("data-pending");
    expect(save.querySelector(".pending-sign")).toBeNull();
    expect(save).toBeDisabled();
    answer.release({ status: 201, body: aSavedQuery({ id: "n5", environment: "dev" }) });
    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
  });

  it("Save shows that it waits, and Save as new waits disabled", async () => {
    const answer = held();
    fakeServer({ [UPDATE]: answer.route });
    const { onSaved, user } = renderSave(OPENED);
    const save = screen.getByRole("button", { name: "Save" });
    const create = screen.getByRole("button", { name: "Save as new" });

    await user.click(save);

    expectPending(save);
    expect(create).not.toHaveAttribute("data-pending");
    expect(create).toBeDisabled();
    answer.release({ status: 200, body: OPENED });
    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
  });

  it("shows the servers 409 word for word and keeps the fields", async () => {
    fakeServer({
      [CREATE]: {
        status: 409,
        body: {
          type: "AlreadyExistsError",
          detail: "You already have a saved query named Late trains in dev.",
        },
      },
    });
    const { onSaved, user } = renderSave();
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.type(name, "Late trains");
    await user.click(screen.getByRole("radio", { name: "Everyone who can run queries in dev" }));

    await user.click(screen.getByRole("button", { name: "Save as new" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You already have a saved query named Late trains in dev.",
    );
    expect(name).toHaveValue("Late trains");
    expect(
      screen.getByRole("radio", { name: "Everyone who can run queries in dev" }),
    ).toBeChecked();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("ignores Escape and Cancel while pending", async () => {
    const answer = held();
    fakeServer({ [CREATE]: answer.route });
    const { onCancel, onSaved, user } = renderSave();
    await user.type(screen.getByRole("textbox", { name: "Name" }), "Late trains");
    await user.click(screen.getByRole("button", { name: "Save as new" }));
    expect(screen.getByRole("status")).toHaveTextContent("Working…");

    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    answer.release({ status: 201, body: aSavedQuery() });
    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
  });

  it("Cancel closes it without a request", async () => {
    const server = fakeServer({});
    const { onCancel, user } = renderSave();

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(server.sent).toEqual([]);
  });

  it("a pasted line break in the name stays one line", async () => {
    const server = fakeServer({ [CREATE]: { status: 201, body: aSavedQuery() } });
    const { user } = renderSave();
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.click(name);

    // An input keeps CR and LF out itself; the line and paragraph separators it lets in.
    await user.paste("Late trains\u2028by line\u2029\u2028and station");

    expect(name).toHaveValue("Late trains by line and station");
    await user.click(screen.getByRole("button", { name: "Save as new" }));
    await waitFor(() => {
      expect(server.sent).toHaveLength(1);
    });
    expect(server.sent[0]?.body).toMatchObject({ name: "Late trains by line and station" });
  });
});
