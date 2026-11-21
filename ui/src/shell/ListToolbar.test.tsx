import { render, renderHook, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useTranslation } from "react-i18next";
import { describe, expect, it, vi } from "vitest";

import { countWords, ListToolbar, useListState } from "./ListToolbar";

describe("ListToolbar", () => {
  it("puts the count and the search on the left, the filters and Refresh on the right", async () => {
    const refresh = vi.fn();
    render(
      <ListToolbar
        count="5 connections"
        search={<input aria-label="Find connections" />}
        filters={<button type="button">All environments</button>}
        onRefresh={refresh}
      />,
    );

    const bar = screen.getByText("5 connections").parentElement;
    expect(bar).toHaveClass("toolbar");
    const end = bar?.querySelector(".toolbar-end");
    expect(end).toContainElement(screen.getByRole("button", { name: "All environments" }));
    expect(end).toContainElement(screen.getByRole("button", { name: "Refresh" }));
    expect(end).not.toContainElement(screen.getByRole("textbox", { name: "Find connections" }));

    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("offers no Refresh without a way to read again, and keeps an empty count's place", () => {
    const { container } = render(<ListToolbar count={null} />);

    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector(".toolbar-count")).toBeEmptyDOMElement();
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
});
