import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CatalogSearch } from "./CatalogSearch";
import { DEFAULT_VIEW, Q_MAX } from "./view";

afterEach(() => {
  vi.useRealTimers();
});

/**
 * Renders the field with fake timers.
 *
 * @param q - The search text the view starts with.
 * @returns The handler and a user-event session.
 */
function field(q = ""): {
  onSearch: ReturnType<typeof vi.fn<(q: string) => void>>;
  type: (value: string) => void;
  rerender: (q: string) => void;
} {
  vi.useFakeTimers();
  const onSearch = vi.fn<(q: string) => void>();
  const { rerender } = render(<CatalogSearch view={{ ...DEFAULT_VIEW, q }} onSearch={onSearch} />);
  return {
    onSearch,
    type: (value) => {
      fireEvent.change(screen.getByRole("searchbox"), { target: { value } });
    },
    rerender: (next) => {
      rerender(<CatalogSearch view={{ ...DEFAULT_VIEW, q: next }} onSearch={onSearch} />);
    },
  };
}

describe("CatalogSearch", () => {
  it("is named by what it searches and limits the text", () => {
    field();

    const box = screen.getByRole("searchbox", { name: "Search tables, columns and descriptions" });
    expect(box).toHaveAttribute("placeholder", "Search tables, columns and descriptions");
    expect(box).toHaveAttribute("maxlength", String(Q_MAX));
  });

  it("shows the text of the view", () => {
    field("store_id");

    expect(screen.getByRole("searchbox")).toHaveValue("store_id");
  });

  it("calls once, 250 ms after typing pauses", () => {
    const { onSearch, type } = field();

    type("s");
    type("store");
    type("store_id");
    expect(onSearch).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(249);
    });
    expect(onSearch).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(1);
    });

    expect(onSearch).toHaveBeenCalledExactlyOnceWith("store_id");
  });

  it("does not call for the text the view already has", () => {
    const { onSearch } = field("store_id");

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(onSearch).not.toHaveBeenCalled();
  });

  it("does not call for a trailing space the address would drop", () => {
    const { onSearch, type } = field("store");

    type("store ");
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(onSearch).not.toHaveBeenCalled();
    expect(screen.getByRole("searchbox")).toHaveValue("store ");
  });

  it("clears with Escape", () => {
    const { onSearch } = field("store_id");

    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Escape" });
    act(() => {
      vi.advanceTimersByTime(250);
    });

    expect(screen.getByRole("searchbox")).toHaveValue("");
    expect(onSearch).toHaveBeenCalledExactlyOnceWith("");
  });

  it("follows the address when it changes from elsewhere", () => {
    const { rerender } = field("store_id");

    rerender("sales");

    expect(screen.getByRole("searchbox")).toHaveValue("sales");
  });

  it("keeps what was typed while the address catches up", () => {
    const { rerender, type } = field();

    type("ab");
    act(() => {
      vi.advanceTimersByTime(250);
    });
    type("abc");
    rerender("ab");

    expect(screen.getByRole("searchbox")).toHaveValue("abc");
  });
});
