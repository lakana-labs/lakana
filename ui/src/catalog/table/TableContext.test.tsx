import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { JSX, ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { AccessOut } from "../../api";
import { MeContext } from "../../shell/me";
import { fakeServer } from "../../test/server";
import { permissions, table } from "../../test/table";
import { TableProvider, useTableData } from "./TableContext";

const PLACE = { environment: "prod", namespace: "gold.sales", name: "fact_sales" };

/**
 * A person with a name and nothing else.
 *
 * @param name - The principal name.
 * @returns What the person may do.
 */
function person(name: string): AccessOut {
  return { name, admin: false, noAccessYet: false, mayDeploy: false, picture: null };
}

/**
 * Provides a table to what is inside, as the person named `name`, who may do `allowed`.
 *
 * @param name - The principal name.
 * @param allowed - The actions the server says the person may do.
 * @param loaded - The table the page read.
 * @returns A wrapper for `renderHook`.
 */
function around(
  name: string,
  allowed: readonly string[],
  loaded = table(),
): ({ children }: { children: ReactNode }) => JSX.Element {
  fakeServer({
    [`GET /api/v1/principals/${name}/permissions`]: {
      status: 200,
      body: permissions(name, allowed),
    },
  });
  return function Wrapper({ children }) {
    return (
      <MeContext value={person(name)}>
        <TableProvider place={PLACE} loaded={loaded}>
          {children}
        </TableProvider>
      </MeContext>
    );
  };
}

describe("TableContext", () => {
  it("lets the owner edit without any permission to drop the table", async () => {
    const { result } = renderHook(() => useTableData(), { wrapper: around("ana.nowak", []) });

    expect(result.current.mayEdit).toBe(true);
    await waitFor(() => {
      expect(result.current.permissions.data?.principal).toBe("ana.nowak");
    });
    expect(result.current.mayEdit).toBe(true);
  });

  it("lets whoever may drop the table edit it, once the permissions arrived", async () => {
    const { result } = renderHook(() => useTableData(), {
      wrapper: around("kim", ["READ_TABLE", "DROP_TABLE"]),
    });

    expect(result.current.mayEdit).toBe(false);
    await waitFor(() => {
      expect(result.current.mayEdit).toBe(true);
    });
  });

  it("does not let a reader edit, and knows where the table is", async () => {
    const { result } = renderHook(() => useTableData(), {
      wrapper: around("kim", ["READ_TABLE", "SUBMIT_QUERY"]),
    });

    await waitFor(() => {
      expect(result.current.permissions.data).toBeDefined();
    });
    expect(result.current.mayEdit).toBe(false);
    expect(result.current.ref).toEqual(PLACE);
  });

  it("does not let anyone edit when the permissions cannot be read", async () => {
    fakeServer({
      "GET /api/v1/principals/kim/permissions": { status: 500, body: { detail: "Broke." } },
    });
    const { result } = renderHook(() => useTableData(), {
      wrapper: ({ children }) => (
        <MeContext value={person("kim")}>
          <TableProvider place={PLACE} loaded={table()}>
            {children}
          </TableProvider>
        </MeContext>
      ),
    });

    await waitFor(() => {
      expect(result.current.permissions.error).toBe("Broke.");
    });
    expect(result.current.mayEdit).toBe(false);
  });

  it("shows a table the server answered after a change, until the table is read again", () => {
    fakeServer({
      "GET /api/v1/principals/kim/permissions": {
        status: 200,
        body: permissions("kim", ["READ_TABLE"]),
      },
    });
    let read = table();
    const { result, rerender } = renderHook(() => useTableData(), {
      wrapper: ({ children }) => (
        <MeContext value={person("kim")}>
          <TableProvider place={PLACE} loaded={read}>
            {children}
          </TableProvider>
        </MeContext>
      ),
    });

    act(() => {
      result.current.replaceTable(table({ description: "Edited." }));
    });
    expect(result.current.table.description).toBe("Edited.");

    read = table({ description: "Read again." });
    rerender();
    expect(result.current.table.description).toBe("Read again.");
  });

  it("says so outside a table's page", () => {
    function Probe(): JSX.Element {
      useTableData();
      return <p>never</p>;
    }
    const quiet = console.error;
    console.error = () => undefined;
    try {
      expect(() => render(<Probe />)).toThrow("useTableData is used outside a table's page.");
    } finally {
      console.error = quiet;
    }
    expect(screen.queryByText("never")).not.toBeInTheDocument();
  });
});
