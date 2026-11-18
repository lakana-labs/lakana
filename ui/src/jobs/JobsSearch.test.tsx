import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { JobsSearch } from "./JobsSearch";

describe("JobsSearch", () => {
  it("hands on each keystroke, and takes words the address brings from elsewhere", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    const { rerender } = render(<JobsSearch query="" onSearch={onSearch} />);

    const field = screen.getByRole("searchbox", { name: "Search jobs" });
    await user.type(field, "ab");
    expect(onSearch.mock.calls).toEqual([["a"], ["ab"]]);

    // The address catching up with the field changes nothing.
    rerender(<JobsSearch query="ab" onSearch={onSearch} />);
    expect(field).toHaveValue("ab");

    // Back brings other words.
    rerender(<JobsSearch query="weekly" onSearch={onSearch} />);
    expect(field).toHaveValue("weekly");
  });
});
