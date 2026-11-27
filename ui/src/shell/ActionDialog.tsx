/**
 * A dialog for one action: centred over the dimmed page, closed by its own buttons, the × in its
 * top corner or Escape.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode } from "react";
import { Dialog, Modal, ModalOverlay } from "react-aria-components";

import { DialogClose } from "./DialogClose";

/** Properties of {@link ActionDialog}. */
export interface ActionDialogProps {
  /** Called when the person closes the dialog with Escape or its ×: what its Cancel does. */
  readonly onClose: () => void;
  /** The content; its `<Heading slot="title">` names the dialog. */
  readonly children: ReactNode;
}

/**
 * Renders the dialog, open while it is mounted. A click outside does not close it, so a link
 * shown once is not lost by a stray click.
 *
 * @param props - The close handler and the content.
 * @param props.onClose - Called on Escape and by the ×.
 * @param props.children - The content.
 * @returns The dialog.
 */
export function ActionDialog({ onClose, children }: ActionDialogProps): JSX.Element {
  return (
    <ModalOverlay className="palette-overlay" isOpen onOpenChange={onClose}>
      <Modal className="float dialog">
        <Dialog className="palette-dialog">
          {children}
          <DialogClose onClose={onClose} />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
