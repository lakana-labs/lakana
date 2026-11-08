/**
 * The command palette (Ctrl/⌘ K): go to any area by typing a few letters of its name.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Autocomplete,
  Dialog,
  Input,
  Menu,
  MenuItem,
  Modal,
  ModalOverlay,
  SearchField,
  useFilter,
} from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import type { ShippedArea } from "./areas";

/** Properties of {@link CommandPalette}. */
export interface CommandPaletteProps {
  /** The areas the user sees: one Go-to command each. */
  readonly areas: readonly ShippedArea[];
  /** Whether the palette is open. */
  readonly isOpen: boolean;
  /** Called to open or close it. */
  readonly onOpenChange: (isOpen: boolean) => void;
}

/**
 * The palette's content, mounted only while it is open, so every opening starts empty.
 *
 * @param props - The areas and the close callback.
 * @param props.areas - The areas the user sees.
 * @param props.onClose - Closes the palette.
 * @returns The search field and the commands.
 */
function PaletteContent({
  areas,
  onClose,
}: {
  readonly areas: readonly ShippedArea[];
  readonly onClose: () => void;
}): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { contains } = useFilter({ sensitivity: "base" });
  const [query, setQuery] = useState("");

  return (
    <Autocomplete filter={contains} inputValue={query} onInputChange={setQuery}>
      {/* The dialog's one field takes focus as it opens (the WAI-ARIA dialog pattern). */}
      {/* eslint-disable-next-line jsx-a11y/no-autofocus -- see above */}
      <SearchField className="palette-field" aria-label={t("shell.search")} autoFocus>
        <Input className="palette-input" placeholder={t("palette.placeholder")} />
      </SearchField>
      <Menu
        className="menu palette-menu"
        aria-label={t("shell.search")}
        renderEmptyState={() => <p className="palette-empty">{t("palette.empty", { query })}</p>}
      >
        {areas.map((area) => (
          <MenuItem
            className="menu-item"
            key={area.id}
            id={area.id}
            onAction={() => {
              onClose();
              void navigate(area.path);
            }}
          >
            {t("palette.goTo", { area: t(`areas.${area.id}.name`) })}
          </MenuItem>
        ))}
      </Menu>
    </Autocomplete>
  );
}

/**
 * Renders the palette as a modal dialog. Escape or a click outside closes it and returns focus
 * to where it was; choosing a command goes there and closes it.
 *
 * @param props - The areas and the open state.
 * @param props.areas - The areas the user sees.
 * @param props.isOpen - Whether the palette is open.
 * @param props.onOpenChange - Called to open or close it.
 * @returns The palette.
 */
export function CommandPalette({ areas, isOpen, onOpenChange }: CommandPaletteProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <ModalOverlay
      className="palette-overlay"
      isOpen={isOpen}
      onOpenChange={onOpenChange}
      isDismissable
    >
      <Modal className="palette float">
        <Dialog className="palette-dialog" aria-label={t("shell.search")}>
          <PaletteContent
            areas={areas}
            onClose={() => {
              onOpenChange(false);
            }}
          />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
