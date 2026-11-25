import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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

/**
 * Lets an animation frame and the timers after it run, after the section's own, which was asked for first.
 */
async function aFrame(): Promise<void> {
  await act(
    () =>
      new Promise((done) => {
        requestAnimationFrame(() => {
          done(undefined);
        });
      }),
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ListSection", () => {
  it("gives the focus to its heading when the row holding it leaves, also when the browser tells the blur", async () => {
    const { rerender } = render(section(true));
    screen.getByRole("button", { name: "Cancel" }).focus();

    // Chrome tells the blur of a removed element; jsdom does not, so the test tells it.
    blurWithoutNext();
    rerender(section(false));

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Running now" })).toHaveFocus();
    });
    await act(() => new Promise((done) => setTimeout(done)));
  });

  it("leaves the focus alone once the person moved it away within the page", async () => {
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
      await aFrame();

      expect(document.activeElement).toBe(document.body);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not take the focus when a frame runs before the timer of a blank click", () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
    try {
      const { rerender } = render(section(true));
      const cancel = screen.getByRole("button", { name: "Cancel" });
      cancel.focus();
      // A render while the focus is in the section schedules a frame ...
      rerender(section(true));
      // ... the person clicks a blank part of the page ...
      blurWithoutNext();
      cancel.blur();
      // ... and the frame comes before the timer that records it.
      act(() => {
        frames.at(-1)?.(0);
      });

      expect(document.activeElement).toBe(document.body);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the focus of a person who went from the section into another dialog, then on to the page, and clicked a blank part", async () => {
    const page = (): React.JSX.Element => (
      <>
        {section(true)}
        <div role="dialog" aria-label="Commands">
          <button type="button">Run</button>
        </div>
        <button type="button">Elsewhere</button>
      </>
    );
    const { rerender } = render(page());
    screen.getByRole("button", { name: "Cancel" }).focus();
    screen.getByRole("button", { name: "Run" }).focus();
    screen.getByRole("button", { name: "Elsewhere" }).focus();
    act(() => {
      screen.getByRole("button", { name: "Elsewhere" }).blur();
    });
    // The next poll renders the section again.
    rerender(page());
    await aFrame();

    expect(document.activeElement).toBe(document.body);
  });

  it("gives the focus to the heading when the row and the question that held it both leave", async () => {
    const page = (open: boolean): React.JSX.Element => (
      <>
        {section(open)}
        {open && (
          <div role="dialog" aria-label="Question">
            <button type="button">Yes</button>
          </div>
        )}
      </>
    );
    const { rerender } = render(page(true));
    screen.getByRole("button", { name: "Cancel" }).focus();
    screen.getByRole("button", { name: "Yes" }).focus();
    // The answer comes: the question closes and the run has left the list.
    rerender(page(false));

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Running now" })).toHaveFocus();
    });
  });

  it("keeps the focus where a person left it from a dialog with a click on a blank part, when the row leaves later", async () => {
    const page = (row: boolean): React.JSX.Element => (
      <>
        {section(row)}
        <div role="dialog" aria-label="Tour step">
          <button type="button">Next</button>
        </div>
      </>
    );
    const { rerender } = render(page(true));
    screen.getByRole("button", { name: "Cancel" }).focus();
    screen.getByRole("button", { name: "Next" }).focus();
    // A click on a blank part of the page, straight from the dialog.
    await act(async () => {
      screen.getByRole("button", { name: "Next" }).blur();
      await new Promise((done) => setTimeout(done));
    });
    rerender(page(false));
    await aFrame();

    expect(document.activeElement).toBe(document.body);
  });

  it("gives the focus to the heading after a second dialog opened over the question and closed again", async () => {
    const page = (open: boolean): React.JSX.Element => (
      <>
        {section(open)}
        {open && (
          <div role="dialog" aria-label="Question">
            <button type="button">Yes</button>
          </div>
        )}
        <div role="dialog" aria-label="Commands">
          <button type="button">Run</button>
        </div>
      </>
    );
    const { rerender } = render(page(true));
    screen.getByRole("button", { name: "Cancel" }).focus();
    screen.getByRole("button", { name: "Yes" }).focus();
    // Ctrl+K, then Escape: the palette hands the focus back to the question.
    screen.getByRole("button", { name: "Run" }).focus();
    screen.getByRole("button", { name: "Yes" }).focus();
    rerender(page(false));

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Running now" })).toHaveFocus();
    });
  });

  it("leaves the focus in a dialog the row's leaving finds it in, and on the page after it closes", async () => {
    const page = (row: boolean, dialog: boolean): React.JSX.Element => (
      <>
        {section(row)}
        {dialog && (
          <div role="dialog" aria-label="Commands">
            <input aria-label="Command" />
          </div>
        )}
      </>
    );
    const { rerender } = render(page(true, true));
    screen.getByRole("button", { name: "Cancel" }).focus();
    screen.getByRole("textbox", { name: "Command" }).focus();
    // The run ends while the palette is open.
    rerender(page(false, true));
    await aFrame();
    expect(screen.getByRole("textbox", { name: "Command" })).toHaveFocus();

    // The palette closes and leaves the focus on the page; the next poll renders the section again.
    rerender(page(false, false));
    await aFrame();
    rerender(page(false, false));
    await aFrame();
    expect(document.activeElement).toBe(document.body);
  });

  it("does not take the focus for a row once the person went to a control elsewhere", async () => {
    const page = (row: boolean): React.JSX.Element => (
      <>
        {section(row)}
        {row && <button type="button">Elsewhere</button>}
      </>
    );
    const { rerender } = render(page(true));
    screen.getByRole("button", { name: "Cancel" }).focus();
    screen.getByRole("button", { name: "Elsewhere" }).focus();
    // Both leave: the focus falls to the page, and it is not the section's doing.
    rerender(page(false));
    await aFrame();

    expect(document.activeElement).toBe(document.body);
  });

  it("still gives the focus to the heading after the window lost it and got it back", async () => {
    const { rerender } = render(section(true));
    screen.getByRole("button", { name: "Cancel" }).focus();
    // A tab switch: a blur that names no next element, the focus staying on the button.
    blurWithoutNext();
    await act(() => new Promise((done) => setTimeout(done)));
    rerender(section(false));

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2, name: "Running now" })).toHaveFocus();
    });
  });
});
