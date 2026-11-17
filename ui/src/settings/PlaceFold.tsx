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
import type { SettingsViewer } from "./places";
import "./settingsStrings";

/** Properties of {@link PlaceFold}. */
export interface PlaceFoldProps {
  /** What the rules know about the person, who is shown only the places they may see. */
  readonly viewer: SettingsViewer;
}

/**
 * Renders the Section ▾ button and, while it is open, the same {@link PlaceList} the wide
 * screen shows beside the place. Choosing a place closes it; Escape closes it and returns the
 * focus to the button.
 *
 * @param props - The person.
 * @param props.viewer - What the rules know about the person.
 * @returns The button and its popover.
 */
export function PlaceFold({ viewer }: PlaceFoldProps): JSX.Element {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);
  // Choosing a place moves to another address: the popover closes with the move.
  // (State adjusted while rendering, as React documents for state that follows a change.)
  const [at, setAt] = useState(pathname);
  if (at !== pathname) {
    setAt(pathname);
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
            <PlaceList viewer={viewer} />
          </Dialog>
        </Popover>
      </DialogTrigger>
    </div>
  );
}
