/**
 * A button that waits for the server: the pending sign from the first moment, no second press.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { Button, type ButtonProps } from "react-aria-components";

/** Properties of {@link PendingButton}. */
export interface PendingButtonProps extends Omit<ButtonProps, "isPending" | "children"> {
  /** The button's label. */
  readonly children: ReactNode;
  /** Whether the request this button started is still open. */
  readonly pending: boolean;
}

/**
 * A React Aria button that, while `pending`, shows a small turning ring before its label. React
 * Aria keeps focus on the button, ignores presses and announces the pending state itself, so
 * the ring is hidden from assistive technology.
 *
 * @param props - The button's own properties and whether it is pending.
 * @param props.children - The label.
 * @param props.pending - Whether the request is still open.
 * @returns The button.
 */
export function PendingButton({ children, pending, ...rest }: PendingButtonProps): JSX.Element {
  return (
    <Button {...rest} isPending={pending}>
      {pending && <span className="pending-sign" aria-hidden="true" />}
      {children}
    </Button>
  );
}
