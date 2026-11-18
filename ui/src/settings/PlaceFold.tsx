/**
 * Settings' list of places on a narrow screen: a Section button that opens it in a popover.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { Button, Dialog, DialogTrigger, Popover } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router";

import { PlaceList } from "./PlaceList";
import "./settingsStrings";

/**
 * Renders the Section ▾ button and, while it is open, the same {@link PlaceList} the wide
 * screen shows beside the place. Choosing a place closes it; Escape closes it and returns the
 * focus to the button.
 *
 * @returns The button and its popover.
 */
export function PlaceFold(): JSX.Element {
  const { t } = useTranslation();
  // Every navigation is a new history entry with its own key, the place already shown included.
  const { key } = useLocation();
  const [open, setOpen] = useState(false);
  // Choosing a place navigates, even to the place already shown: the popover closes with it.
  // (State adjusted while rendering, as React documents for state that follows a change.)
  const [at, setAt] = useState(key);
  if (at !== key) {
    setAt(key);
    setOpen(false);
  }
  return (
    <div className="place-fold">
      <DialogTrigger isOpen={open} onOpenChange={setOpen}>
        <Button className="button">
          {t("settings.places.picker")}
          <span aria-hidden="true">▾</span>
        </Button>
        <Popover className="float place-fold-popover" placement="bottom start">
          <Dialog className="place-fold-dialog" aria-label={t("settings.places.label")}>
            <PlaceList />
          </Dialog>
        </Popover>
      </DialogTrigger>
    </div>
  );
}
