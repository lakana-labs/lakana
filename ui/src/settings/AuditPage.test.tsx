import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { areasFor } from "../shell/areas";
import { anEvent, AUDIT_EVENTS, SQL_SHA256 } from "../test/audit";
import { renderShell } from "../test/render";
import { expectServerSorts } from "../test/sorting";
import type { FakeRoute } from "../test/server";

const PATH = "/settings/audit";
const ROUTE = "GET /api/v1/audit-events";
const ENVIRONMENTS = [
  { name: "dev", is_protected: false },
  { name: "prod", is_protected: true },
];

/**
 * The audit route answering one page, and the environments.
 *
 * @param items - The events.
 * @param next - The next page's cursor.
 * @returns The routes.
 */
function answering(
  items: readonly unknown[] = AUDIT_EVENTS,
  next: string | null = null,
): Record<string, FakeRoute> {
  return {
    [ROUTE]: { status: 200, body: { items, next_cursor: next } },
    "GET /api/v1/environments": { status: 200, body: ENVIRONMENTS },
    "GET /api/v1/principals": {
      status: 200,
      body: [{ name: "maria", type: "USER", is_active: true, picture: null }],
    },
  };
}

/**
 * The region of the list, once it is read.
 *
 * @returns The region.
 */
async function events(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Audit events" });
}

/**
 * The query of each audit read sent so far.
 *
 * @param sent - The requests.
 * @returns Each read's parameters.
 */
function reads(sent: readonly { readonly path: string }[]): URLSearchParams[] {
  return sent
    .filter((request) => request.path.startsWith("/api/v1/audit-events"))
    .map((request) => new URL(request.path, "http://lakana.test").searchParams);
}

/**
 * The row of an event, by a text in it.
 *
 * @param region - The list's region.
 * @param text - A text of the row.
 * @returns The row.
 */
function rowWith(region: HTMLElement, text: string): HTMLElement {
  return within(region).getByText(text).closest("tr") as HTMLElement;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("AuditPage", () => {
  it("lists the events newest first, from the last 30 days, with who and what they were on", async () => {
    const { server } = renderShell(PATH, { routes: answering() });

    const region = await events();
    expect(screen.getByRole("heading", { level: 1, name: "Audit log" })).toBeInTheDocument();
    expect(screen.getByText("5 events")).toBeInTheDocument();
    const heads = within(region)
      .getAllByRole("columnheader")
      .map((head) => head.textContent);
    expect(heads).toEqual(["When▼", "Event", "Who", "On", "Details"]);
    expect(within(region).getByRole("columnheader", { name: /When/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    const grant = rowWith(region, "GRANT_CREATED");
    expect(within(grant).getByRole("link", { name: "maria" })).toHaveAttribute(
      "href",
      "/settings/access/people/maria",
    );
    expect(within(grant).getByText("dev")).toBeInTheDocument();
    const write = rowWith(region, "TABLE_WRITE");
    expect(write).toHaveTextContent("dev · gold.station_ranking");
    const failed = rowWith(region, "AUTHENTICATION_FAILED");
    expect(failed).toHaveTextContent("No one");
    expect(failed).toHaveTextContent("Nothing");
    expect(failed).toHaveTextContent("None");
    expect(within(failed).queryByRole("button")).not.toBeInTheDocument();
    const newer = rowWith(region, "WIDGET_FROBNICATED (not known to this release)");
    expect(newer).toHaveTextContent("The instance");

    const [first] = reads(server.sent);
    expect(first?.get("limit")).toBe("50");
    expect(first?.has("sort")).toBe(false);
    const since = Date.parse(first?.get("since") ?? "");
    expect(Date.now() - since).toBeGreaterThan(29.9 * 86_400_000);
    expect(Date.now() - since).toBeLessThan(30.1 * 86_400_000);
  });

  it("shows an event's fields as the server sent them, naming what it hid", async () => {
    const { user } = renderShell(PATH, { routes: answering() });
    const region = await events();

    const query = rowWith(region, "QUERY_COMPLETED");
    const show = within(query).getByRole("button", { name: /^Show details of QUERY_COMPLETED/ });
    expect(show).toHaveAttribute("aria-expanded", "false");
    await user.click(show);
    const hide = within(query).getByRole("button", { name: /^Hide details of QUERY_COMPLETED/ });
    expect(hide).toHaveAttribute("aria-expanded", "true");
    const facts = document.getElementById(hide.getAttribute("aria-controls") ?? "");
    expect(facts?.tagName).toBe("DL");
    expect([...(facts?.querySelectorAll("dt") ?? [])].map((name) => name.textContent)).toEqual([
      "sql_text",
      "tables_touched",
      "row_count",
    ]);
    expect(facts).toHaveTextContent(`Hidden: SHA-256 ${SQL_SHA256.slice(0, 12)}…, 57 characters`);
    expect(facts).toHaveTextContent('["gold.station_ranking"]');
    expect(facts).toHaveTextContent("12");

    const write = rowWith(region, "TABLE_WRITE");
    await user.click(within(write).getByRole("button", { name: /^Show details/ }));
    const opened = within(write).getByRole("button", { name: /^Hide details/ });
    const writeFacts = document.getElementById(opened.getAttribute("aria-controls") ?? "");
    expect(writeFacts?.closest("tr")?.previousElementSibling).toBe(write);
    expect(within(writeFacts as HTMLElement).getByText("Hidden")).toBeInTheDocument();
    expect(within(writeFacts as HTMLElement).getByText("none")).toBeInTheDocument();

    await user.click(hide);
    expect(within(query).queryByRole("term")).not.toBeInTheDocument();
  });

  it("shows the stored type of an event this release does not know, with its hidden characters", async () => {
    const unknown = anEvent("u1", {
      event_type: "UNKNOWN",
      event_type_raw: "WIDGET‮FROBNICATED",
      detail: {},
    });
    renderShell(PATH, { routes: answering([unknown]) });
    const region = await events();

    const type = within(region).getByText(/not known to this release/);
    expect(type).toHaveTextContent("WIDGETU+202EFROBNICATED (not known to this release)");
    expect(type.querySelectorAll(".char-mark")).toHaveLength(1);
  });

  it("sorts by When on the server, the oldest first, and keeps it in the address", async () => {
    const { server, user, router } = renderShell(PATH, { routes: answering() });
    const region = await events();

    await user.click(within(region).getByRole("button", { name: "When" }));

    await waitFor(() => {
      expect(router.state.location.search).toBe("?sort=occurred&order=asc");
    });
    await waitFor(() => {
      expect(reads(server.sent).at(-1)?.get("order")).toBe("asc");
    });
    expect(reads(server.sent).at(-1)?.get("sort")).toBe("occurred");
  });

  it("sorts by event, who and what it is on, on the server; the details do not sort", async () => {
    const { server, user } = renderShell(PATH, { routes: answering() });
    await events();
    const region = (): HTMLElement => screen.getByRole("region", { name: "Audit events" });
    expect(within(region()).queryByRole("button", { name: "Details" })).toBeNull();
    await expectServerSorts(user, region, server.sent, "/api/v1/audit-events", [
      ["Event", "sort=event&order=asc"],
      ["Who", "sort=who&order=asc"],
      ["On", "sort=on&order=asc"],
    ]);
  });

  it("filters by who, event, environment and period, in the address", async () => {
    const { server, user, router } = renderShell(PATH, { routes: answering() });
    await events();

    await user.type(screen.getByRole("combobox", { name: "Who" }), "maria{Enter}{Escape}");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?who=maria");
    });
    await waitFor(() => {
      expect(reads(server.sent).at(-1)?.get("principal_name")).toBe("maria");
    });

    const eventField = screen.getByRole("combobox", { name: "Event" });
    await user.type(eventField, "grant_created{Enter}{Escape}");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?who=maria&event=GRANT_CREATED");
    });
    await waitFor(() => {
      expect(reads(server.sent).at(-1)?.get("event_type")).toBe("GRANT_CREATED");
    });
    expect(eventField).toHaveValue("GRANT_CREATED");

    await user.click(screen.getByRole("button", { name: /Environment/ }));
    await user.click(await screen.findByRole("option", { name: /prod/ }));
    await waitFor(() => {
      expect(reads(server.sent).at(-1)?.get("environment")).toBe("prod");
    });

    await user.click(screen.getByRole("button", { name: /Period/ }));
    await user.click(await screen.findByRole("option", { name: "All time" }));
    await waitFor(() => {
      expect(router.state.location.search).toContain("period=all");
    });
    await waitFor(() => {
      expect(reads(server.sent).at(-1)?.has("since")).toBe(false);
    });
  });

  it("keeps the suggestions closed once Enter applied a name", async () => {
    const { user, router } = renderShell(PATH, { routes: answering() });
    await events();
    const who = screen.getByRole("combobox", { name: "Who" });

    await user.type(who, "maria");
    await screen.findAllByRole("option");
    await user.keyboard("{Escape}");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(router.state.location.search).toBe("?who=maria");
    });
    // The address catches up a moment after the send; the old name must not come back meanwhile.
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(who).toHaveAttribute("aria-expanded", "false");
    expect(who).toHaveValue("maria");
  });

  it("empties a typed name on Clear filters, though the field's blur applies it once more", async () => {
    const { user, router } = renderShell(PATH, { routes: answering([]) });
    await screen.findByText("No events yet.");
    const who = screen.getByRole("combobox", { name: "Who" });

    await user.type(who, "maria");
    await screen.findAllByRole("option");
    await user.keyboard("{Escape}");
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?who=maria");
    });
    await user.click(await screen.findByRole("button", { name: "Clear filters" }));

    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(who).toHaveValue("");
  });

  it("refuses an event type the server does not know, and sends nothing for it", async () => {
    const { server, user, router } = renderShell(PATH, { routes: answering() });
    await events();
    const before = reads(server.sent).length;

    await user.type(screen.getByRole("combobox", { name: "Event" }), "WIDGET{Enter}");

    expect(
      await screen.findByText("No event type named “WIDGET”. Pick one of the suggestions."),
    ).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
    expect(reads(server.sent)).toHaveLength(before);
  });

  it("suggests the event types that contain what is typed", async () => {
    const { user } = renderShell(PATH, { routes: answering() });
    await events();

    await user.type(screen.getByRole("combobox", { name: "Event" }), "passkey");

    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "PASSKEY_ADDED",
      "PASSKEY_REMOVED",
      "PASSKEY_DISABLED",
    ]);
    await user.click(options[1] as HTMLElement);
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Event" })).toHaveValue("PASSKEY_REMOVED");
    });
  });

  it("reads the filters from the address, and Clear filters empties them", async () => {
    const { server, user, router } = renderShell(
      `${PATH}?who=ben&event=TABLE_WRITE&environment=gone&period=7d`,
      { routes: answering([]) },
    );

    expect(await screen.findByText("No event matches these filters.")).toBeInTheDocument();
    const [first] = reads(server.sent);
    expect(first?.get("principal_name")).toBe("ben");
    expect(first?.get("event_type")).toBe("TABLE_WRITE");
    expect(first?.get("environment")).toBe("gone");
    expect(screen.getByRole("combobox", { name: "Who" })).toHaveValue("ben");

    await user.click(screen.getByRole("button", { name: "Clear filters" }));

    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Who" })).toHaveValue("");
    });
  });

  it("says so when the log has no events", async () => {
    renderShell(PATH, { routes: answering([]) });

    expect(await screen.findByText("No events yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });

  it("reads the next page with its cursor and the same filters", async () => {
    const pageOne = [anEvent("a1")];
    const pageTwo = [anEvent("a2", { occurred_at: "2026-11-20T10:00:00Z" })];
    const { server, user } = renderShell(`${PATH}?who=maria`, {
      routes: {
        ...answering(),
        [ROUTE]: (request) => ({
          status: 200,
          body: request.path.includes("cursor=c2")
            ? { items: pageTwo, next_cursor: null }
            : { items: pageOne, next_cursor: "c2" },
        }),
      },
    });
    await events();
    expect(screen.getByText("The first 1 event")).toBeInTheDocument();
    // An open event's fields are a row of their own: the new event is still found.
    await user.click(screen.getByRole("button", { name: /^Show details of GRANT_CREATED/ }));

    await user.click(screen.getByRole("button", { name: "Show more" }));

    expect(await screen.findByText("2 events")).toBeInTheDocument();
    // The focus moves to the first new event, not to the page.
    await waitFor(() => {
      expect(screen.getAllByRole("link", { name: "maria" })[1]).toHaveFocus();
    });
    const last = reads(server.sent).at(-1);
    expect(last?.get("cursor")).toBe("c2");
    expect(last?.get("principal_name")).toBe("maria");
  });

  it("moves the focus to the list when a further page is empty, or its event has no control", async () => {
    const nobody = anEvent("n1", {
      principal_id: null,
      principal_name: null,
      event_type: "AUTHENTICATION_FAILED",
      event_type_raw: "AUTHENTICATION_FAILED",
      detail: {},
    });
    const answers: Record<string, unknown> = {
      first: { items: [anEvent("a1")], next_cursor: "c2" },
      c2: { items: [nobody], next_cursor: "c3" },
      c3: { items: [], next_cursor: null },
    };
    const { user } = renderShell(PATH, {
      routes: {
        ...answering(),
        [ROUTE]: (request) => ({
          status: 200,
          body: answers[/cursor=(c\d)/.exec(request.path)?.[1] ?? "first"],
        }),
      },
    });
    const region = await events();

    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByText("AUTHENTICATION_FAILED")).toBeInTheDocument();
    await waitFor(() => {
      expect(region).toHaveFocus();
    });
    await user.click(screen.getByRole("button", { name: "Show more" }));
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(region).toHaveFocus();
    });
  });

  it("an emptied name and the first period take their filters out of the address", async () => {
    const { user, router } = renderShell(`${PATH}?who=ben&period=7d`, { routes: answering() });
    await events();

    await user.clear(screen.getByRole("combobox", { name: "Who" }));
    await user.keyboard("{Enter}");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?period=7d");
    });
    await user.click(screen.getByRole("button", { name: /Period/ }));
    await user.click(await screen.findByRole("option", { name: "Last 30 days" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
  });

  it("says when no principal or event type matches what is typed", async () => {
    const { user } = renderShell(PATH, {
      routes: { ...answering(), "GET /api/v1/principals": { status: 200, body: [] } },
    });
    await events();

    await user.type(screen.getByRole("combobox", { name: "Who" }), "zoe");
    expect(
      await screen.findByText("No principal named “zoe”. Check the spelling."),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.type(screen.getByRole("combobox", { name: "Event" }), "frobnicate");
    expect(
      await screen.findByText("No event type named “frobnicate”. Pick one of the suggestions."),
    ).toBeInTheDocument();
  });

  it("shows the server's refusal with Try again", async () => {
    renderShell(PATH, {
      routes: {
        ...answering(),
        [ROUTE]: {
          status: 503,
          body: {
            type: "AuditUnavailable",
            detail: "The read could not be recorded in the audit log. Try again in a moment.",
          },
        },
      },
    });

    expect(await screen.findByText(/could not be recorded in the audit log/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("a person who is not an administrator cannot use the Audit log", async () => {
    const { server } = renderShell(PATH, {
      access: areasFor(false),
      me: { name: "kim", admin: false },
      routes: answering(),
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: "You cannot use Audit log" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Audit log" })).not.toBeInTheDocument();
    expect(server.sent.some((request) => request.path.startsWith("/api/v1/audit-events"))).toBe(
      false,
    );
  });
});
