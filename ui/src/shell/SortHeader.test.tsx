import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SortHeader } from "./SortHeader";

/**
 * Renders one header in a table.
 *
 * @param props - The header's properties.
 * @param props.field - The column.
 * @param props.sorted - The sorted column and its direction.
 * @param props.sorted.field - The sorted column.
 * @param props.sorted.order - Its direction.
 * @param props.pressed - Whether it was pressed last.
 * @returns The handler.
 */
function renderHeader({
  field = "name",
  sorted = { field: "name", order: "asc" } as const,
  pressed = false,
}: {
  field?: "name" | "created";
  sorted?: { field: "name" | "created" | null; order: "asc" | "desc" };
  pressed?: boolean;
} = {}) {
  const onSort = vi.fn();
  render(
    <table>
      <thead>
        <tr>
          <SortHeader field={field} sort={sorted} onSort={onSort} pressed={pressed}>
            Name
          </SortHeader>
        </tr>
      </thead>
    </table>,
  );
  return onSort;
}

describe("SortHeader", () => {
  it("says the sorted column's direction with aria-sort and an arrow", () => {
    renderHeader();
    const header = screen.getByRole("columnheader", { name: /Name/u });
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(header).toHaveTextContent("Name▲");
  });

  it("says a descending sort", () => {
    renderHeader({ sorted: { field: "name", order: "desc" } });
    expect(screen.getByRole("columnheader")).toHaveAttribute("aria-sort", "descending");
    expect(screen.getByRole("columnheader")).toHaveTextContent("▼");
  });

  it("says nothing on a column that is not sorted", () => {
    renderHeader({ sorted: { field: "created", order: "desc" } });
    expect(screen.getByRole("columnheader")).not.toHaveAttribute("aria-sort");
    expect(screen.getByRole("columnheader")).toHaveTextContent(/^Name$/u);
  });

  it("sorts by the pointer and by the keyboard", async () => {
    const onSort = renderHeader();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Name/u }));
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(onSort.mock.calls).toEqual([["name"], ["name"], ["name"]]);
  });

  it("takes the focus back when drawn again after its press, and only then", () => {
    renderHeader({ pressed: true });
    expect(screen.getByRole("button", { name: /Name/u })).toHaveFocus();
  });

  it("leaves the focus alone when it was not pressed", () => {
    renderHeader();
    expect(document.body).toHaveFocus();
  });
});
