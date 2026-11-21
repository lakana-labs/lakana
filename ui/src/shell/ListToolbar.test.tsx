import { render, renderHook, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useTranslation } from "react-i18next";
import { describe, expect, it, vi } from "vitest";

import { expectPending } from "../test/pending";

import { countWords, ListToolbar, useListState, useSteadyMore } from "./ListToolbar";

describe("ListToolbar", () => {
  it("reads the count first, then the search, the filters and Refresh, in that order", async () => {
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(
      <ListToolbar
        count="5 connections"
        search={<input aria-label="Find connections" />}
        filters={<button type="button">All environments</button>}
        onRefresh={refresh}
      />,
    );
    const count = screen.getByText("5 connections");
    const search = screen.getByRole("textbox", { name: "Find connections" });
    expect(count.compareDocumentPosition(search) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await user.tab();
    expect(search).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "All environments" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("shows that Refresh waits while the list is read, and takes no second press", async () => {
    const refresh = vi.fn();
    const user = userEvent.setup();
    render(<ListToolbar count="5 connections" onRefresh={refresh} refreshing />);
    const button = screen.getByRole("button", { name: /^Refresh/ });

    expectPending(button);
    await user.click(button);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("offers no Refresh without a way to read again, and says no count while it is read", () => {
    render(<ListToolbar count={null} search={<input aria-label="Find people" />} />);

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(/\d/)).toBeNull();
  });

  it("has no count at all for a list whose sections count their own rows", () => {
    const { container } = render(<ListToolbar search={<input aria-label="Search jobs" />} />);

    expect(container.querySelector("p")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Search jobs" })).toBeInTheDocument();
  });

  it("says the count in words, or the first rows while more can be read", () => {
    const { result } = renderHook(() => useTranslation());
    const { t } = result.current;

    expect(countWords(t, "connections.list", undefined)).toBeNull();
    expect(countWords(t, "access.people", 1)).toBe("1 person");
    expect(countWords(t, "access.people", 5)).toBe("5 people");
    expect(countWords(t, "access.people", 100, true)).toBe("The first 100 people");
  });

  it("keeps the reported state, and the same object for the same state", () => {
    const { result } = renderHook(() => useListState());
    const first = result.current[0];
    expect(first).toEqual({ shown: undefined, more: false, busy: true });

    act(() => {
      result.current[1]({ shown: undefined, more: false, busy: true });
    });
    expect(result.current[0]).toBe(first);

    act(() => {
      result.current[1]({ shown: 3, more: true, busy: false });
    });
    expect(result.current[0]).toEqual({ shown: 3, more: true, busy: false });
  });

  it("keeps the last finished read's answer about more rows while a read is on its way", () => {
    const { result, rerender } = renderHook(({ more, busy }) => useSteadyMore(more, busy), {
      initialProps: { more: true, busy: false },
    });
    expect(result.current).toBe(true);

    rerender({ more: false, busy: true });
    expect(result.current).toBe(true);

    rerender({ more: false, busy: false });
    expect(result.current).toBe(false);
  });
});
