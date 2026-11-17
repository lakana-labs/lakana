/**
 * The Queries screen's library on a narrow screen: a Queries button that opens it in a popover.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button, Dialog, DialogTrigger, Popover } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router";

import { Library, type LibraryProps } from "./Library";
import "./queryStrings";

/**
 * Renders the Queries ▾ button and, while it is open, the same {@link Library} the wide screen
 * shows beside the editor. Opening a row closes it; Escape closes it and returns the focus to
 * the button.
 *
 * @param props - The library's properties.
 * @returns The button and its popover.
 */
export function LibraryFold(props: LibraryProps): JSX.Element {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);
  // A row opened in the popover moves to another address: the popover closes with the move.
  // (State adjusted while rendering, as React documents for state that follows a change.)
  const [at, setAt] = useState(pathname);
  if (at !== pathname) {
    setAt(pathname);
    setOpen(false);
  }
  return (
    <DialogTrigger isOpen={open} onOpenChange={setOpen}>
      <Button className="button">
        {t("queries.library.fold")}
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float queries-fold" placement="bottom start">
        <Dialog className="queries-fold-dialog" aria-label={t("queries.library.label")}>
          <Library {...props} />
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}
