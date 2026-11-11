/**
 * The command palette (Ctrl/⌘ K): go to any area, take the tour again or see the keyboard
 * shortcuts, by typing a few letters of the command.
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

import { useTour } from "../tour/TourProvider";
import type { ShippedArea } from "./areas";

/** Properties of {@link CommandPalette}. */
export interface CommandPaletteProps {
  /** The areas the user sees: one Go-to command each. */
  readonly areas: readonly ShippedArea[];
  /** Whether the palette is open. */
  readonly isOpen: boolean;
  /** Called to open or close it. */
  readonly onOpenChange: (isOpen: boolean) => void;
  /** Opens the keyboard shortcuts. */
  readonly onShortcuts: () => void;
}

/** One command of the palette. */
interface Command {
  /** Its id in the menu. */
  readonly id: string;
  /** What it says. */
  readonly label: string;
  /** What it does, after the palette closed. */
  readonly run: () => void;
}

/** Properties of {@link PaletteContent}. */
interface PaletteContentProps {
  /** The areas the user sees. */
  readonly areas: readonly ShippedArea[];
  /** Closes the palette. */
  readonly onClose: () => void;
  /** Opens the keyboard shortcuts. */
  readonly onShortcuts: () => void;
}

/**
 * The palette's content, mounted only while it is open, so every opening starts empty.
 *
 * @param props - The areas and the callbacks.
 * @param props.areas - The areas the user sees.
 * @param props.onClose - Closes the palette.
 * @param props.onShortcuts - Opens the keyboard shortcuts.
 * @returns The search field and the commands.
 */
function PaletteContent({ areas, onClose, onShortcuts }: PaletteContentProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const tour = useTour();
  const { contains } = useFilter({ sensitivity: "base" });
  const [query, setQuery] = useState("");
  const commands: readonly Command[] = [
    ...areas.map((area) => ({
      id: area.id,
      label: t("palette.goTo", { area: t(`areas.${area.id}.name`) }),
      run: () => {
        void navigate(area.path);
      },
    })),
    {
      id: "tour",
      label: t("help.tour"),
      run: () => {
        tour.start();
      },
    },
    { id: "shortcuts", label: t("help.shortcuts"), run: onShortcuts },
  ];
  const matches = commands.filter((command) => contains(command.label, query)).length;
  let status = "";
  if (matches === 0) {
    status = t("palette.empty", { query });
  } else if (query !== "") {
    status = t("palette.results", { count: matches });
  }

  return (
    <Autocomplete filter={contains} inputValue={query} onInputChange={setQuery}>
      {/* The dialog's one field takes focus as it opens (the WAI-ARIA dialog pattern). */}
      {/* eslint-disable-next-line jsx-a11y/no-autofocus -- see above */}
      <SearchField className="palette-field" aria-label={t("shell.search")} autoFocus>
        <Input className="palette-input" placeholder={t("palette.placeholder")} />
      </SearchField>
      <Menu
        className="menu palette-menu"
        aria-label={t("palette.commands")}
        renderEmptyState={() => <p className="palette-empty">{t("palette.empty", { query })}</p>}
      >
        {commands.map(({ id, label, run }) => (
          <MenuItem
            className="menu-item"
            key={id}
            id={id}
            onAction={() => {
              onClose();
              run();
            }}
          >
            {label}
          </MenuItem>
        ))}
      </Menu>
      {/* Mounted with the dialog, so screen readers announce every change (WCAG 4.1.3). */}
      <p className="visually-hidden" role="status">
        {status}
      </p>
    </Autocomplete>
  );
}

/**
 * Renders the palette as a modal dialog. Escape or a click outside closes it and returns focus
 * to where it was; choosing a command closes it and does it.
 *
 * @param props - The areas, the open state and the shortcuts action.
 * @param props.areas - The areas the user sees.
 * @param props.isOpen - Whether the palette is open.
 * @param props.onOpenChange - Called to open or close it.
 * @param props.onShortcuts - Opens the keyboard shortcuts.
 * @returns The palette.
 */
export function CommandPalette({
  areas,
  isOpen,
  onOpenChange,
  onShortcuts,
}: CommandPaletteProps): JSX.Element {
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
            onShortcuts={onShortcuts}
          />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
