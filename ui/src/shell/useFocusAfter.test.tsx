import { render, screen, waitFor } from "@testing-library/react";
import type { JSX } from "react";
import { describe, expect, it } from "vitest";

import { useFocusAfter } from "./useFocusAfter";

function Panel({
  change,
  armed,
}: {
  readonly change: number;
  readonly armed: boolean;
}): JSX.Element {
  const { target, arm } = useFocusAfter<HTMLHeadingElement>(change);
  if (armed) arm();
  return (
    <div>
      <h2 ref={target} tabIndex={-1}>
        Passkeys
      </h2>
      <button type="button">Elsewhere</button>
    </div>
  );
}

function nextFrame(): Promise<void> {
  return new Promise((resolve) =>
    requestAnimationFrame(() => {
      resolve();
    }),
  );
}

describe("useFocusAfter", () => {
  it("moves focus after a closing dialog has handed it back, not before", async () => {
    const { rerender } = render(<Panel change={0} armed={false} />);
    const elsewhere = screen.getByRole("button", { name: "Elsewhere" });
    // A closing React Aria dialog restores focus to its trigger in the next animation frame.
    requestAnimationFrame(() => {
      elsewhere.focus();
    });
    rerender(<Panel change={1} armed />);

    await nextFrame();
    await nextFrame();
    expect(screen.getByRole("heading", { name: "Passkeys" })).toHaveFocus();
  });

  it("moves nothing when it was not armed", async () => {
    const { rerender } = render(<Panel change={0} armed={false} />);
    rerender(<Panel change={1} armed={false} />);
    await nextFrame();
    expect(document.body).toHaveFocus();
  });

  it("forgets a pending move when the component goes away", async () => {
    const { rerender, unmount } = render(<Panel change={0} armed={false} />);
    rerender(<Panel change={1} armed />);
    unmount();
    await waitFor(() => {
      expect(document.body).toHaveFocus();
    });
  });
});
