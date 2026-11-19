import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { LivePoll } from "../shell/useLivePoll";
import { ListSection, RowEnd } from "./ListSection";

const POLL: LivePoll<unknown> = {
  data: [],
  error: null,
  failure: null,
  loading: false,
  reload: vi.fn(),
  stale: false,
  failures: 0,
} as unknown as LivePoll<unknown>;

/**
 * The section with one row and its Cancel, or with none.
 *
 * @param row - Whether the row is there.
 * @returns The section.
 */
function section(row: boolean): React.JSX.Element {
  return (
    <ListSection
      heading="Running now"
      poll={POLL}
      loading="Reading the runs"
      rowCount={row ? 1 : 0}
      empty="Nothing is running."
      region="Runs running now"
      scroll="jobs-scroll"
      head={<th scope="col">Name</th>}
      note={null}
    >
      {row && (
        <tr>
          <td>nightly_sales</td>
          <RowEnd>
            <button type="button">Cancel</button>
          </RowEnd>
        </tr>
      )}
    </ListSection>
  );
}

/**
 * Tells the focused element's blur as a browser does when the element leaves the page with the
 * focus in it: a `focusout` that names no next element.
 */
function blurWithoutNext(): void {
  act(() => {
    document.activeElement?.dispatchEvent(
      new FocusEvent("focusout", { bubbles: true, relatedTarget: null }),
    );
  });
}

describe("ListSection", () => {
  it("gives the focus to its heading when the row holding it leaves, also when the browser tells the blur", async () => {
    const { rerender } = render(section(true));
    screen.getByRole("button", { name: "Cancel" }).focus();

    // Chrome tells the blur of a removed element; jsdom does not, so the test tells it.
    blurWithoutNext();
    rerender(section(false));

    expect(screen.getByRole("heading", { level: 2, name: "Running now" })).toHaveFocus();
    await act(() => new Promise((done) => setTimeout(done)));
  });

  it("leaves the focus alone once the person moved it away within the page", () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      const { rerender } = render(section(true));
      const cancel = screen.getByRole("button", { name: "Cancel" });
      cancel.focus();

      // A click on a blank part of the page: the focus goes to the page, the row stays.
      blurWithoutNext();
      cancel.blur();
      act(() => {
        vi.runAllTimers();
      });
      rerender(section(false));

      expect(document.activeElement).toBe(document.body);
    } finally {
      vi.useRealTimers();
    }
  });
});
