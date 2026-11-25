import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { areasFor } from "../shell/areas";
import {
  aColumnMask,
  aMember,
  anEnvironment,
  aRowFilter,
  freshSession,
  managedScopesRoute,
  STALE_SESSION,
  STEP_UP_ROUTES,
} from "../test/access";
import { item } from "../test/catalog";
import { held } from "../test/pending";
import { renderShell } from "../test/render";
import { type FakeRoute, whoami } from "../test/server";
import { fakeWebAuthn } from "../test/webauthn";

const PATH = "/settings/access/filters-and-masks";

/**
 * The routes of Row filters & masks: dev and prod, nothing managed, one policy of each kind.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
function policyRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...managedScopesRoute(),
    "GET /api/v1/sign-in/session": freshSession(),
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), anEnvironment("prod")],
    },
    "GET /api/v1/row-filters": { status: 200, body: [aRowFilter()] },
    "GET /api/v1/column-masks": { status: 200, body: [aColumnMask()] },
    "GET /api/v1/principals": { status: 200, body: [] },
    "GET /api/v1/groups": { status: 200, body: [] },
    ...more,
  };
}

/**
 * The paths of the requests to one list.
 *
 * @param sent - The requests.
 * @param list - `row-filters` or `column-masks`.
 * @returns Their paths.
 */
function listReads(sent: readonly { method: string; path: string }[], list: string): string[] {
  return sent
    .filter((request) => request.method === "GET" && request.path.startsWith(`/api/v1/${list}`))
    .map((request) => request.path);
}

describe("FiltersAndMasksPage", () => {
  it("reads the filter from the address", async () => {
    const { server } = renderShell(`${PATH}?group=analysts&environment=dev`, {
      routes: policyRoutes(),
    });

    await screen.findByRole("region", { name: "Row filters" });
    await screen.findByRole("region", { name: "Column masks" });
    for (const list of ["row-filters", "column-masks"]) {
      expect(listReads(server.sent, list)).toEqual([
        `/api/v1/${list}?group_name=analysts&environment=dev&limit=50`,
      ]);
    }
    expect(screen.getByRole("combobox", { name: "Name" })).toHaveValue("analysts");
  });

  it("shows each policy's who, table, expression and when", async () => {
    renderShell(PATH, { routes: policyRoutes() });

    const filters = await screen.findByRole("region", { name: "Row filters" });
    const [, filter] = within(filters).getAllByRole("row");
    expect(within(filter as HTMLElement).getByRole("link", { name: "maria" })).toHaveAttribute(
      "href",
      "/settings/access/people/maria",
    );
    expect(
      within(filter as HTMLElement).getByRole("link", { name: "dev › gold › station_ranking" }),
    ).toHaveAttribute("href", "/catalog/dev/gold/station_ranking/access");
    expect(filter?.querySelector("code.expression")).toHaveTextContent("region = 'EU'");
    const masks = await screen.findByRole("region", { name: "Column masks" });
    const [head, mask] = within(masks).getAllByRole("row");
    expect(head).toHaveTextContent("WhoOnColumnExpressionCreated");
    expect(mask).toHaveTextContent("group analystsdev › gold › station_rankingemail'***'");
  });

  it("a new filter drops the old rows and the old error at once", async () => {
    const anyone = held();
    const nobody = {
      status: 404,
      body: { detail: "No principal named 'nobody'.", type: "NotFoundError" },
    };
    const { user } = renderShell(`${PATH}?principal=nobody`, {
      routes: policyRoutes({
        "GET /api/v1/row-filters": (request) =>
          request.path.includes("principal_name") ? nobody : anyone.route(),
        "GET /api/v1/column-masks": (request) =>
          request.path.includes("principal_name") ? nobody : { status: 200, body: [] },
      }),
    });
    await waitFor(() => {
      expect(screen.getAllByText("No principal named 'nobody'.")).toHaveLength(2);
    });

    await user.click(screen.getByRole("button", { name: /Who$/ }));
    await user.click(await screen.findByRole("option", { name: "Anyone" }));

    expect(screen.queryByText("No principal named 'nobody'.")).toBeNull();
    anyone.release({ status: 200, body: [aRowFilter()] });
    expect(await screen.findByRole("link", { name: "maria" })).toBeInTheDocument();
  });

  it("shows more with the next cursor, per list", async () => {
    const { user } = renderShell(PATH, {
      routes: policyRoutes({
        "GET /api/v1/column-masks": (request) =>
          request.path.includes("cursor=c2")
            ? { status: 200, body: [aColumnMask({ id: "m-2", group: "ops" })] }
            : { status: 200, body: [aColumnMask()], headers: { "X-Next-Cursor": "c2" } },
      }),
    });

    await user.click(await screen.findByRole("button", { name: "Show more" }));

    const masks = screen.getByRole("region", { name: "Column masks" });
    await waitFor(() => {
      expect(within(masks).getAllByRole("row")).toHaveLength(3);
    });
    expect(masks).toHaveTextContent("group ops");
  });

  it("marks a managed environment's policies", async () => {
    renderShell(PATH, {
      routes: policyRoutes({
        ...managedScopesRoute(["prod"], "team.yaml"),
        "GET /api/v1/row-filters": {
          status: 200,
          body: [aRowFilter(), aRowFilter({ id: "f-2", environment: "prod" })],
        },
      }),
    });

    const filters = await screen.findByRole("region", { name: "Row filters" });
    const [, dev, prod] = within(filters).getAllByRole("row");
    expect(
      await within(dev as HTMLElement).findByRole("button", { name: /^Delete/ }),
    ).toBeVisible();
    expect(prod).toHaveTextContent("⌂ GitManaged by team.yaml");
    expect(within(prod as HTMLElement).queryByRole("button")).toBeNull();
    expect(screen.getByRole("button", { name: "Add filter or mask" })).toBeInTheDocument();
  });

  it("a managed instance offers no write", async () => {
    const { server } = renderShell(PATH, {
      routes: policyRoutes(managedScopesRoute(["instance"])),
    });

    expect(await screen.findByText(/The whole instance is managed by/)).toBeInTheDocument();
    expect(
      screen.getByText('lakana row-filter revoke ROW_FILTER_ID --override-managed --reason "WHY"'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'lakana column-mask revoke COLUMN_MASK_ID --override-managed --reason "WHY"',
      ),
    ).toBeInTheDocument();
    await screen.findByRole("region", { name: "Column masks" });
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add filter or mask" })).toBeNull();
    expect(server.sent.some((request) => request.path.includes("override_managed"))).toBe(false);
  });

  it("offers no write until the managed scopes are read", async () => {
    const scopes = held();
    renderShell(PATH, {
      routes: policyRoutes({ "GET /api/v1/access/managed-scopes": scopes.route }),
    });

    await screen.findByRole("region", { name: "Row filters" });
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add filter or mask" })).toBeNull();
    scopes.release({ status: 200, body: [] });
    expect(await screen.findByRole("button", { name: "Add filter or mask" })).toBeInTheDocument();
  });

  it("a failed read of them offers none, with Try again", async () => {
    let fail = true;
    const { user } = renderShell(PATH, {
      routes: policyRoutes({
        "GET /api/v1/access/managed-scopes": () =>
          fail
            ? { status: 500, body: { detail: "The database is down." } }
            : { status: 200, body: [] },
      }),
    });

    expect(await screen.findByText(/The database is down\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findAllByRole("button", { name: /^Delete/ })).toHaveLength(2);
    expect(screen.getByRole("heading", { level: 1, name: "Row filters & masks" })).toHaveFocus();
  });

  it("deletes after the passkey touch, asked first", async () => {
    fakeWebAuthn();
    let deleted = false;
    const { user, server } = renderShell(PATH, {
      routes: policyRoutes({
        "GET /api/v1/sign-in/session": STALE_SESSION,
        ...STEP_UP_ROUTES,
        "DELETE /api/v1/row-filters/f-1": () => {
          deleted = true;
          return { status: 204 };
        },
        "GET /api/v1/row-filters": () => ({
          status: 200,
          body: deleted ? [aRowFilter({ id: "f-2", environment: "prod" })] : [aRowFilter()],
        }),
      }),
    });

    await user.click(
      await screen.findByRole("button", {
        name: "Delete the row filter on dev › gold › station_ranking for maria",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Delete this row filter?" });
    expect(dialog).toHaveTextContent(
      "maria then sees every row of dev › gold › station_ranking that their grants allow.",
    );
    const from = server.sent.length;
    await user.click(within(dialog).getByRole("button", { name: "Delete the row filter" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await waitFor(() => {
      expect(server.sent.length).toBeGreaterThanOrEqual(from + 5);
    });
    expect(
      server.sent.slice(from, from + 5).map((request) => `${request.method} ${request.path}`),
    ).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/sign-in/step-up/options",
      "POST /api/v1/sign-in/step-up",
      "DELETE /api/v1/row-filters/f-1",
      "GET /api/v1/row-filters?limit=50",
    ]);
    expect(screen.getByText("Deleted.")).toBeInTheDocument();
  });

  it("a cancelled touch deletes nothing", async () => {
    const webauthn = fakeWebAuthn();
    webauthn.get.mockRejectedValue(new DOMException("Cancelled.", "NotAllowedError"));
    const { user, server } = renderShell(PATH, {
      routes: policyRoutes({ "GET /api/v1/sign-in/session": STALE_SESSION, ...STEP_UP_ROUTES }),
    });

    await user.click(
      await screen.findByRole("button", {
        name: "Delete the mask of email on dev › gold › station_ranking for analysts",
      }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Delete this column mask?" });
    expect(dialog).toHaveTextContent(
      "analysts then sees email of dev › gold › station_ranking as it is stored.",
    );
    await user.click(within(dialog).getByRole("button", { name: "Delete the column mask" }));

    expect(await within(dialog).findByRole("alert")).toBeVisible();
    expect(server.sent.some((request) => request.method === "DELETE")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("the focus goes to the section when its last policy goes", async () => {
    let deleted = false;
    const { user } = renderShell(PATH, {
      routes: policyRoutes({
        "DELETE /api/v1/column-masks/m-2": () => {
          deleted = true;
          return { status: 204 };
        },
        "GET /api/v1/column-masks": (request) => {
          if (deleted) return { status: 200, body: [] };
          return request.path.includes("cursor=c2")
            ? { status: 200, body: [aColumnMask({ id: "m-2", group: "ops" })] }
            : { status: 200, body: [aColumnMask()], headers: { "X-Next-Cursor": "c2" } };
        },
      }),
    });
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    await user.click(await screen.findByRole("button", { name: /^Delete the mask .* for ops$/ }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete the column mask" }));

    expect(await screen.findByText("No column mask yet.")).toBeInTheDocument();
    await waitFor(() => {
      // The list's count is the panel's heading, and takes the focus.
      expect(screen.getByRole("heading", { level: 2, name: "0 column masks" })).toHaveFocus();
    });
  });

  it("a policy manager who is not an administrator sees marks, no links", async () => {
    const { server } = renderShell(PATH, {
      access: areasFor(false),
      me: { name: "pat", admin: false },
      routes: policyRoutes({
        ...managedScopesRoute(["dev"]),
        "GET /api/v1/whoami": { status: 200, body: whoami("pat", false, true, false, [], true) },
      }),
    });

    const filters = await screen.findByRole("region", { name: "Row filters" });
    await waitFor(() => {
      expect(filters).toHaveTextContent("⌂ Git");
    });
    expect(within(filters).getByText("maria").closest("a")).toBeNull();
    expect(server.sent.some((request) => request.path.startsWith("/api/v1/access/status"))).toBe(
      false,
    );
  });

  it("marks hidden characters and keeps a long expression whole", async () => {
    const long = `region IN (${"'EU', ".repeat(570)}'US')`.slice(0, 4000);
    renderShell(PATH, {
      routes: policyRoutes({
        "GET /api/v1/row-filters": {
          status: 200,
          body: [
            aRowFilter({
              principal: { id: "x", name: "an‮al", principal_type: "user" },
              expression: long,
            }),
          ],
        },
        "GET /api/v1/column-masks": {
          status: 200,
          body: [aColumnMask({ group: null, column: "e​mail" })],
        },
      }),
    });

    const filters = await screen.findByRole("region", { name: "Row filters" });
    expect(filters).toHaveAttribute("tabindex", "0");
    expect(filters).toHaveTextContent("anU+202Eal");
    const code = filters.querySelector("code.expression");
    expect(code?.textContent).toBe(long);
    expect(code?.closest("[title]")).toBeNull();
    const masks = await screen.findByRole("region", { name: "Column masks" });
    expect(masks).toHaveTextContent("eU+200Bmail");
    expect(masks).toHaveTextContent("a removed person or group group-1");
    expect(
      screen.getByRole("button", {
        name: "Delete the mask of eU+200Bmail on dev › gold › station_ranking for a removed person or group",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Delete the row filter on dev › gold › station_ranking for anU+202Eal",
      }),
    ).toBeInTheDocument();
  });

  it("suggests the tables of the chosen environment as namespace.table, and a choice applies", async () => {
    const { server, user, router } = renderShell(`${PATH}?environment=dev`, {
      routes: policyRoutes({
        "GET /api/v1/tables": {
          status: 200,
          body: {
            items: [
              item({ environment: "dev", namespace: "gold", name: "station_ranking" }),
              item({ environment: "dev", namespace: "silver.weather", name: "station_daily" }),
            ],
            next_cursor: null,
            capped: false,
          },
        },
      }),
    });
    const table = await screen.findByRole("combobox", { name: "Table (namespace.table)" });
    expect(table).toHaveAccessibleDescription(
      "Type part of its name, or its namespace and a dot, to see the tables you may read; applied on Enter.",
    );

    await user.type(table, "station");
    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.getAttribute("aria-label"))).toEqual([
      "gold.station_ranking",
      "silver.weather.station_daily",
    ]);
    await user.click(options[1] as HTMLElement);

    expect(table).toHaveValue("silver.weather.station_daily");
    await waitFor(() => {
      expect(router.state.location.search).toBe(
        "?environment=dev&table=silver.weather.station_daily",
      );
    });
    expect(table).toHaveFocus();
    expect(listReads(server.sent, "row-filters")).toContain(
      "/api/v1/row-filters?environment=dev&namespace=silver.weather&table=station_daily&limit=50",
    );

    await user.clear(table);
    await user.type(table, "gold.sta");
    await screen.findAllByRole("option");
    expect(listReads(server.sent, "tables")).toEqual([
      "/api/v1/tables?environment=dev&sort=name&limit=20&name=station",
      "/api/v1/tables?environment=dev&sort=name&limit=20&namespace=gold&name=sta",
    ]);
  });

  it("says when no table matches in the environment", async () => {
    const { user } = renderShell(`${PATH}?environment=prod`, {
      routes: policyRoutes({
        "GET /api/v1/tables": {
          status: 200,
          body: { items: [], next_cursor: null, capped: false },
        },
      }),
    });

    await user.type(
      await screen.findByRole("combobox", { name: "Table (namespace.table)" }),
      "gold.secret_salaries",
    );

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "No table named “gold.secret_salaries” that you may read in prod.",
      );
    });
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("suggests no table for an environment typed by a person who may list none", async () => {
    const { server, user, router } = renderShell(`${PATH}?environment=dev`, {
      routes: policyRoutes({ "GET /api/v1/environments": { status: 200, body: [] } }),
    });
    const table = await screen.findByRole("combobox", { name: "Table (namespace.table)" });
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "Environment" })).toHaveValue("dev");
    });

    await user.type(table, "gold.station_ranking{ArrowDown}{Enter}");

    expect(router.state.location.search).toBe("?environment=dev&table=gold.station_ranking");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(listReads(server.sent, "tables")).toEqual([]);
  });

  it("suggests principals or groups for Who to an administrator, and a choice applies", async () => {
    const { server, user, router } = renderShell(PATH, {
      routes: policyRoutes({
        "GET /api/v1/principals": { status: 200, body: [aMember("maria"), aMember("marek")] },
        "GET /api/v1/groups": { status: 200, body: [{ name: "marketing" }] },
      }),
    });
    await user.click(await screen.findByRole("button", { name: /Who$/ }));
    await user.click(await screen.findByRole("option", { name: "A person" }));
    const name = screen.getByRole("combobox", { name: "Name" });

    await user.type(name, "mar");
    await user.click(await screen.findByRole("option", { name: "marek, Person" }));

    await waitFor(() => {
      expect(router.state.location.search).toBe("?principal=marek");
    });
    // The field stays, with the focus in it.
    expect(name).toHaveFocus();
    await user.click(screen.getByRole("button", { name: /Who$/ }));
    await user.click(await screen.findByRole("option", { name: "A group" }));
    await waitFor(() => {
      expect(screen.queryByRole("option", { name: "A group" })).toBeNull();
    });
    await user.click(name);
    await user.keyboard("{ArrowDown}");
    expect(await screen.findByRole("option", { name: "marketing" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /^marek/ })).toBeNull();
    expect(listReads(server.sent, "principals?")).toEqual(["/api/v1/principals?limit=20&q=mar"]);
  });

  it("keeps the focus in Who's Name once a typed name applies", async () => {
    const { user, router } = renderShell(PATH, { routes: policyRoutes() });
    await user.click(await screen.findByRole("button", { name: /Who$/ }));
    await user.click(await screen.findByRole("option", { name: "A group" }));
    const name = screen.getByRole("combobox", { name: "Name" });

    await user.type(name, "analysts{Enter}");

    await waitFor(() => {
      expect(router.state.location.search).toBe("?group=analysts");
    });
    expect(name).toHaveFocus();
    expect(name).toHaveValue("analysts");
  });

  it("keeps Who's Name a plain field for a policy manager who may not list principals", async () => {
    const { server, user, router } = renderShell(PATH, {
      access: areasFor(false),
      me: { name: "pat", admin: false },
      routes: policyRoutes({
        "GET /api/v1/whoami": { status: 200, body: whoami("pat", false, true, false, [], true) },
      }),
    });
    await user.click(await screen.findByRole("button", { name: /Who$/ }));
    await user.click(await screen.findByRole("option", { name: "A group" }));

    await user.type(screen.getByRole("combobox", { name: "Name" }), "analysts{ArrowDown}{Enter}");

    expect(router.state.location.search).toBe("?group=analysts");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(
      server.sent.some((request) => /^\/api\/v1\/(principals|groups)\?/.test(request.path)),
    ).toBe(false);
  });

  it("Clear filters goes back to every policy", async () => {
    const { user, router } = renderShell(`${PATH}?environment=prod`, {
      routes: policyRoutes({
        "GET /api/v1/row-filters": (request) => ({
          status: 200,
          body: request.path.includes("environment") ? [] : [aRowFilter()],
        }),
        "GET /api/v1/column-masks": { status: 200, body: [] },
      }),
    });

    expect(await screen.findByText("No row filter matches these filters.")).toBeInTheDocument();
    expect(await screen.findByText("No column mask matches these filters.")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0] as HTMLElement);

    expect(await screen.findByRole("link", { name: "maria" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
    expect(screen.getByRole("heading", { level: 1, name: "Row filters & masks" })).toHaveFocus();
    expect(await screen.findByText("No column mask yet.")).toBeInTheDocument();
  });

  it("reads the row filters again on Refresh, keeping the filter", async () => {
    const { server, user } = renderShell(`${PATH}?group=analysts&environment=dev`, {
      routes: policyRoutes(),
    });
    await screen.findByRole("region", { name: "Row filters" });
    const reads = (): number =>
      server.sent.filter(
        (request) =>
          request.method === "GET" &&
          request.path === "/api/v1/row-filters?group_name=analysts&environment=dev&limit=50",
      ).length;
    await waitFor(() => {
      expect(reads()).toBeGreaterThan(0);
    });
    const before = reads();

    await user.click(screen.getAllByRole("button", { name: "Refresh" })[0] as HTMLElement);

    await waitFor(() => {
      expect(reads()).toBeGreaterThan(before);
    });
  });
});
