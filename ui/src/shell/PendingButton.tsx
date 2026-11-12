/**
 * A button that waits for the server: the pending sign from the first moment, no second press.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useId } from "react";
import { Button, type ButtonProps, ProgressBar } from "react-aria-components";
import { useTranslation } from "react-i18next";

/** Properties of {@link PendingButton}. */
export interface PendingButtonProps extends Omit<ButtonProps, "isPending" | "children"> {
  /** The button's label. */
  readonly children: ReactNode;
  /** Whether the request this button started is still open. */
  readonly pending: boolean;
}

/**
 * A React Aria button that, while `pending`, shows a small turning ring before its label. React
 * Aria keeps focus on the button, ignores presses and, when the button has focus, announces its
 * label plus the indeterminate progress bar inside it. The ring is that progress bar, named
 * "Working…", so a screen reader hears "Save, Working…". The label is named for the button
 * unless the caller gives an `aria-label` or `aria-labelledby`, because React Aria adds the
 * progress bar only to a name it can already refer to.
 *
 * @param props - The button's own properties and whether it is pending.
 * @param props.children - The label.
 * @param props.pending - Whether the request is still open.
 * @returns The button.
 */
export function PendingButton({ children, pending, ...rest }: PendingButtonProps): JSX.Element {
  const { t } = useTranslation();
  const labelId = useId();
  const named = rest["aria-label"] !== undefined || rest["aria-labelledby"] !== undefined;
  return (
    <Button {...rest} {...(named ? {} : { "aria-labelledby": labelId })} isPending={pending}>
      {pending && (
        <ProgressBar className="pending-sign" isIndeterminate aria-label={t("common.working")} />
      )}
      {named ? children : <span id={labelId}>{children}</span>}
    </Button>
  );
}
