/**
 * The × in a dialog's top corner: one way out that needs no scrolling to the dialog's foot.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

/** Properties of {@link DialogClose}. */
export interface DialogCloseProps {
  /** Does what the dialog's Cancel (or Close) and Escape do. */
  readonly onClose: () => void;
}

/**
 * Renders the close button. It comes last in the dialog, so Tab reaches the dialog's own fields
 * first, and stands in the top corner (`.close-button dialog-close`, shell/screen.css).
 *
 * @param props - The close handler.
 * @param props.onClose - Does what Cancel and Escape do.
 * @returns The button.
 */
export function DialogClose({ onClose }: DialogCloseProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Button className="close-button dialog-close" aria-label={t("dialog.close")} onPress={onClose}>
      <span aria-hidden="true">×</span>
    </Button>
  );
}
