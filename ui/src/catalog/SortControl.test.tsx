import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SortControl } from "./SortControl";
import { DEFAULT_VIEW } from "./view";

describe("SortControl", () => {
  it("offers every column and Created in Sort by", async () => {
    const user = userEvent.setup();
    render(<SortControl view={DEFAULT_VIEW} onChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /Sort by/ }));

    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "Name",
      "Owner",
      "Health",
      "Rows",
      "Size",
      "Last write",
      "Last read",
      "Created",
    ]);
  });

  it("applies the default order of a chosen key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SortControl view={DEFAULT_VIEW} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: /Sort by/ }));
    await user.click(screen.getByRole("option", { name: "Created" }));

    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_VIEW, sort: "created", order: "desc" });
  });

  it("says the order in words for the sorted key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SortControl
        view={{ ...DEFAULT_VIEW, sort: "created", order: "desc" }}
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("button", { name: /Order/ })).toHaveTextContent("Newest first");
    await user.click(screen.getByRole("button", { name: /Order/ }));
    expect(
      within(screen.getByRole("listbox"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Newest first", "Oldest first"]);
    await user.click(screen.getByRole("option", { name: "Oldest first" }));

    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_VIEW, sort: "created", order: "asc" });
  });

  it.each([
    ["name", "A to Z", "Z to A"],
    ["health", "Worst first", "Best first"],
    ["rows", "Largest first", "Smallest first"],
    ["last_read", "Newest first", "Oldest first"],
  ] as const)("words the order of %s", async (sort, desc, asc) => {
    const user = userEvent.setup();
    const view = { ...DEFAULT_VIEW, sort, order: "desc" } as const;
    render(<SortControl view={view} onChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /Order/ }));

    const words = within(screen.getByRole("listbox"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(words.sort()).toEqual([asc, desc].sort());
  });
});
