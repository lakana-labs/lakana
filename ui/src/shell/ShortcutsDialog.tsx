/**
 * The keyboard shortcuts, opened from Help or the command palette.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button, Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";
import { useTranslation } from "react-i18next";

import { shortcutLabel } from "./shortcut";

/** The shortcuts, in the order a person meets them; each `shortcuts.<id>` in `en.json`. */
const SHORTCUTS = ["search", "escape", "tab", "enter"] as const;

/** Properties of {@link ShortcutsDialog}. */
export interface ShortcutsDialogProps {
  /** Closes the dialog; focus returns to where it was before. */
  readonly onClose: () => void;
}

/**
 * Lists the shortcuts as keys and what they do. Close and Escape close it; a click outside does
 * too, since it holds nothing to lose.
 *
 * @param props - The close handler.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function ShortcutsDialog({ onClose }: ShortcutsDialogProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <ModalOverlay className="palette-overlay" isOpen isDismissable onOpenChange={onClose}>
      <Modal className="float dialog">
        <Dialog className="palette-dialog">
          <Heading slot="title" level={2}>
            {t("shortcuts.title")}
          </Heading>
          <dl className="shortcuts">
            {SHORTCUTS.map((id) => (
              <div key={id}>
                <dt>
                  <kbd>
                    {id === "search"
                      ? t(`shell.searchShortcut.${shortcutLabel(navigator)}`)
                      : t(`shortcuts.${id}.keys`)}
                  </kbd>
                </dt>
                <dd>{t(`shortcuts.${id}.text`)}</dd>
              </div>
            ))}
          </dl>
          <div className="step-actions">
            <Button className="button" onPress={onClose}>
              {t("shortcuts.close")}
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
