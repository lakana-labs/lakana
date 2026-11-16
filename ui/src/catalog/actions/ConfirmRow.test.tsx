import { render, screen } from "@testing-library/react";
import type { JSX } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { useAttempt } from "../../sign-in/Attempt";
import { ConfirmRow, useConfirm } from "./ConfirmRow";

/** Properties of {@link Row}. */
interface RowProps {
  /** Whether the dialog still waits, for whether the environment is protected. */
  readonly waiting: boolean;
  /** Whether the confirm button cannot be pressed. */
  readonly disabled: boolean;
  /** The action. */
  readonly onConfirm: () => void;
}

/**
 * A dialog's confirm row, as the dialogs build it.
 *
 * @param props - The row's state.
 * @param props.waiting - Whether the dialog still waits.
 * @param props.disabled - Whether the confirm button cannot be pressed.
 * @param props.onConfirm - The action.
 * @returns The row.
 */
function Row({ waiting, disabled, onConfirm }: RowProps): JSX.Element {
  const attempt = useAttempt();
  const confirm = useConfirm({ waiting, busy: false, disabled, onConfirm });
  return (
    <ConfirmRow
      attempt={attempt}
      label="Delete the namespace"
      confirm={confirm}
      onCancel={vi.fn()}
    />
  );
}

const CHECKING = "Checking whether the environment is protected…";

describe("ConfirmRow", () => {
  it("acts at once when nothing is awaited", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(<Row waiting={false} disabled={false} onConfirm={onConfirm} />);

    await user.click(screen.getByRole("button", { name: "Delete the namespace" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("keeps a press made while the dialog waits, says so, and acts once the wait is over", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Row waiting disabled={false} onConfirm={onConfirm} />);
    const button = screen.getByRole("button", { name: "Delete the namespace" });

    await user.click(button);

    expect(onConfirm).not.toHaveBeenCalled();
    expect(button).toHaveAttribute("data-pending", "true");
    expect(screen.getByRole("status")).toHaveTextContent(CHECKING);

    rerender(<Row waiting={false} disabled={false} onConfirm={onConfirm} />);

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(button).not.toHaveAttribute("data-pending");
    expect(screen.getByRole("status")).toHaveTextContent("");
    rerender(<Row waiting={false} disabled={false} onConfirm={onConfirm} />);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("drops a kept press when the answer makes the button unpressable, such as a reason now needed", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Row waiting disabled={false} onConfirm={onConfirm} />);

    await user.click(screen.getByRole("button", { name: "Delete the namespace" }));
    rerender(<Row waiting={false} disabled onConfirm={onConfirm} />);

    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Delete the namespace" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("keeps no press made while the button cannot be pressed, such as Enter in an empty field", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    /**
     * A field's Enter, which calls the hook's press directly.
     *
     * @param props - The state.
     * @param props.waiting - Whether the dialog still waits.
     * @param props.disabled - Whether the button cannot be pressed.
     * @returns A button that stands for Enter.
     */
    function Enter({ waiting, disabled }: { waiting: boolean; disabled: boolean }): JSX.Element {
      const { press } = useConfirm({ waiting, busy: false, disabled, onConfirm });
      return (
        <button type="button" onClick={press}>
          Enter
        </button>
      );
    }
    const { rerender } = render(<Enter waiting disabled />);

    await user.click(screen.getByRole("button", { name: "Enter" }));
    rerender(<Enter waiting={false} disabled={false} />);

    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("ignores a second press while the action runs, such as Enter after a click", async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    /**
     * A field's Enter while the action already runs.
     *
     * @returns A button that stands for Enter.
     */
    function Busy(): JSX.Element {
      const { press } = useConfirm({ busy: true, disabled: false, onConfirm });
      return (
        <button type="button" onClick={press}>
          Enter
        </button>
      );
    }
    render(<Busy />);

    await user.click(screen.getByRole("button", { name: "Enter" }));

    expect(onConfirm).not.toHaveBeenCalled();
  });
});
