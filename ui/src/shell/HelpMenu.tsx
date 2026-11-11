/**
 * Help, at the bottom of the sidebar: the tour again, the documentation, the keyboard shortcuts,
 * Get started brought back, and the version of the server.
 *
 * @packageDocumentation
 */
import { type JSX, useRef, useState } from "react";
import { Button, Menu, MenuItem, MenuTrigger, Popover } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { problemText } from "../sign-in/messages";
import { useTour } from "../tour/TourProvider";
import { DOCS_URL, HOW_TO_URL, TUTORIAL_URL } from "./docs";
import { useInterfaceState } from "./InterfaceState";

/** The documentation the menu opens, each in a new tab. */
const DOCS = [
  ["getting-started", "help.gettingStarted", TUTORIAL_URL],
  ["how-to", "help.howTo", HOW_TO_URL],
  ["docs", "help.documentation", DOCS_URL],
] as const;

/** Properties of {@link HelpMenu}. */
export interface HelpMenuProps {
  /** Opens the keyboard shortcuts. */
  readonly onShortcuts: () => void;
}

/**
 * Renders Help. "Show “Get started” again" is there only while the person has hidden the list;
 * it brings the list back and goes Home, where it is. The version is text, not an item.
 *
 * @param props - The shortcuts handler.
 * @param props.onShortcuts - Opens the keyboard shortcuts.
 * @returns The Help button and its menu, and what went wrong when bringing the list back failed.
 */
export function HelpMenu({ onShortcuts }: HelpMenuProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const tour = useTour();
  const { state, change, version } = useInterfaceState();
  const button = useRef<HTMLButtonElement>(null);
  const [error, setError] = useState<string | null>(null);
  const showGetStarted = (): void => {
    setError(null);
    change({ get_started_hidden: false }).then(
      () => {
        void navigate("/");
      },
      (failure: unknown) => {
        setError(t("help.showFailed", { detail: problemText(t, failure) }));
      },
    );
  };
  return (
    <div className="user-menu">
      <MenuTrigger>
        <Button className="user-menu-button" ref={button} data-tour="help">
          {t("help.title")}
        </Button>
        <Popover className="float" placement="top start">
          <Menu className="menu" aria-label={t("help.title")}>
            <MenuItem
              className="menu-item"
              id="tour"
              onAction={() => {
                tour.start(button.current);
              }}
            >
              {t("help.tour")}
            </MenuItem>
            {DOCS.map(([id, label, href]) => (
              <MenuItem
                className="menu-item"
                key={id}
                id={id}
                href={href}
                target="_blank"
                rel="noreferrer"
                textValue={t(label)}
              >
                {t(label)}
                <span className="visually-hidden"> {t("home.newTab")}</span>
              </MenuItem>
            ))}
            <MenuItem className="menu-item" id="shortcuts" onAction={onShortcuts}>
              {t("help.shortcuts")}
            </MenuItem>
            {state?.get_started_hidden === true && (
              <MenuItem className="menu-item" id="get-started" onAction={showGetStarted}>
                {t("help.showGetStarted")}
              </MenuItem>
            )}
          </Menu>
          {version !== null && <p className="menu-foot">{t("help.version", { version })}</p>}
        </Popover>
      </MenuTrigger>
      {error !== null && (
        <p className="user-menu-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
