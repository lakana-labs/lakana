import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TableListItemOut } from "../api";
import { item, PAGE } from "../test/catalog";
import { TableList } from "./TableList";
import { DEFAULT_VIEW, type CatalogView } from "./view";

function show(
  items: readonly TableListItemOut[] = PAGE.items,
  view: CatalogView = DEFAULT_VIEW,
  onSort: (sort: string) => void = vi.fn(),
  busy = false,
): void {
  render(
    <MemoryRouter>
      <TableList items={items} view={view} onSort={onSort} busy={busy} />
    </MemoryRouter>,
  );
}

function row(name: string): HTMLElement {
  return screen.getByRole("link", { name }).closest("tr") as HTMLElement;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-13T09:30:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("TableList", () => {
  it("shows exactly the columns Name, Owner, Health, Rows, Size, Last write and Last read", () => {
    show();

    const heads = screen.getAllByRole("columnheader").map((head) => head.textContent.trim());
    expect(heads.slice(0, 7).map((head) => head.replace(/[▲▼]/u, "").trim())).toEqual([
      "Name",
      "Owner",
      "Health",
      "Rows",
      "Size",
      "Last write",
      "Last read",
    ]);
    expect(heads).toHaveLength(8);
    expect(screen.queryByRole("columnheader", { name: /Created/ })).not.toBeInTheDocument();
  });

  it("opens the table page from every row", () => {
    show();

    expect(screen.getByRole("link", { name: "fact_sales" })).toHaveAttribute(
      "href",
      "/catalog/prod/gold.sales/fact_sales",
    );
    for (const body of screen.getAllByRole("row").slice(1)) {
      expect(within(body).getByRole("link")).toBeInTheDocument();
      expect(within(body).getByText("›")).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("names kind, namespace and environment on the second line", () => {
    show([...PAGE.items, item({ kind: "VIEW", name: "station_view", model_kind: null })]);

    const fact = row("fact_sales");
    expect(within(fact).getByText("Fact · gold.sales")).toBeInTheDocument();
    expect(within(fact).getByText("prod")).toHaveClass("env-label");
    expect(within(row("station_view")).getByText("View · gold")).toBeInTheDocument();
    expect(within(row("dim_store")).getByText("Dimension · gold")).toBeInTheDocument();
    expect(within(row("station_ranking")).getByText("Table · gold")).toBeInTheDocument();
  });

  it("shows the owner's initials and name, and says when it is a service account", () => {
    show();

    const kim = row("station_ranking");
    expect(within(kim).getByText("KS")).toBeInTheDocument();
    expect(within(kim).getByText("kim.schulz")).toBeInTheDocument();
    expect(within(kim).queryByText("Service account")).not.toBeInTheDocument();
    expect(within(row("dim_store")).getByText("Service account")).toBeInTheDocument();
  });

  it("says what is not known in every cell", () => {
    show([
      item({
        kind: "VIEW",
        owner: null,
        row_count: null,
        size_bytes: null,
        last_write_at: null,
        last_read_at: null,
        health_summary: { state: "not_judged", item: "not_judged", text: "Not judged", others: 0 },
      }),
    ]);

    const only = row("station_ranking");
    expect(within(only).getAllByText("–")).toHaveLength(6);
    expect(within(only).getByText("No owner")).toHaveClass("visually-hidden");
    expect(within(only).getAllByText("Not known")).toHaveLength(2);
    expect(within(only).getByText("Never written")).toBeInTheDocument();
    expect(within(only).getByText("Never read")).toBeInTheDocument();
    expect(within(only).getByText("Not judged")).toBeInTheDocument();
  });

  it("right-aligns rows and size", () => {
    show();

    expect(screen.getByText("48,210").closest("td")).toHaveClass("number");
    expect(screen.getByText("3.1 MB").closest("td")).toHaveClass("number");
    expect(screen.getByText("kim.schulz").closest("td")).not.toHaveClass("number");
  });

  it("shortens a million and keeps the exact number for a screen reader", () => {
    show();

    const sales = row("fact_sales");
    expect(within(sales).getByText("1.2M")).toHaveAttribute("aria-hidden", "true");
    expect(within(sales).getByText("1,204,000")).toHaveClass("visually-hidden");
    expect(within(sales).getByText("98.3 kB")).toBeInTheDocument();
  });

  it("shows times relative and the absolute time in words", () => {
    show();

    const when = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" }).format(
      new Date("2026-11-13T07:30:00Z"),
    );
    const read = within(row("station_ranking")).getByText("2 hours ago");
    expect(read).toHaveAttribute("title", when);
    expect(within(row("station_ranking")).getByText(when)).toHaveClass("visually-hidden");
    expect(within(row("station_ranking")).getByText("1 day ago")).toBeInTheDocument();
  });

  it("says just now for a time under a minute", () => {
    show([item({ last_write_at: "2026-11-13T09:29:40Z" })]);

    expect(screen.getByText("just now")).toBeInTheDocument();
  });

  it("sorts by a header and says which one is sorted", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const onSort = vi.fn();
    show(PAGE.items, { ...DEFAULT_VIEW, sort: "rows", order: "desc" }, onSort);

    expect(screen.getByRole("columnheader", { name: /Rows/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(screen.getByRole("columnheader", { name: /Rows/ })).toHaveTextContent("▼");
    expect(screen.getByRole("columnheader", { name: /Name/ })).not.toHaveAttribute("aria-sort");
    await user.click(screen.getByRole("button", { name: "Owner" }));

    expect(onSort).toHaveBeenCalledWith("owner");
  });

  it("marks the list busy while a new view loads", () => {
    show(PAGE.items, DEFAULT_VIEW, vi.fn(), true);

    expect(screen.getByRole("region", { name: "Tables" })).toHaveAttribute("aria-busy", "true");
  });
});
