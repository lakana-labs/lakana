/**
 * The menu under the user's name: theme, density and language.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import {
  Button,
  Header,
  Menu,
  MenuItem,
  MenuSection,
  MenuTrigger,
  Popover,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import { usePreferences } from "../preferences/PreferencesProvider";
import type { Density, Language, Preferences, Theme } from "../preferences/preferences";

const THEMES: readonly Theme[] = ["system", "light", "dark"];
const DENSITIES: readonly Density[] = ["roomy", "compact"];
const LANGUAGES: readonly Language[] = ["en"];

/**
 * The i18n key of a value's name.
 *
 * @param names - The key prefix.
 * @param option - The value.
 * @returns The key.
 */
function nameKey(names: string, option: string): string {
  return `${names}.${option}`;
}

/** Properties of {@link ChoiceSection}. */
interface ChoiceSectionProps<K extends keyof Preferences> {
  /** The preference the section chooses. */
  readonly field: K;
  /** The section's heading. */
  readonly label: string;
  /** The values, in menu order. */
  readonly options: readonly Preferences[K][];
  /** The i18n key prefix of the values' names. */
  readonly names: string;
}

/**
 * One section of single-choice items for one preference.
 *
 * @param props - The preference, the heading and the values.
 * @param props.field - The preference.
 * @param props.label - The heading.
 * @param props.options - The values.
 * @param props.names - The i18n key prefix of the values' names.
 * @returns The section.
 */
function ChoiceSection<K extends keyof Preferences>({
  field,
  label,
  options,
  names,
}: ChoiceSectionProps<K>): JSX.Element {
  const { t } = useTranslation();
  const [preferences, update] = usePreferences();
  return (
    <MenuSection
      className="menu-section"
      selectionMode="single"
      disallowEmptySelection
      selectedKeys={[preferences[field]]}
    >
      <Header className="menu-header">{label}</Header>
      {options.map((option) => (
        <MenuItem
          className="menu-item"
          key={option}
          id={option}
          onAction={() => {
            update({ [field]: option });
          }}
        >
          {/* The check mark is drawn here, not by ::before on the item, so it stays out of the name. */}
          <span className="menu-check" aria-hidden="true" />
          {t(nameKey(names, option))}
        </MenuItem>
      ))}
    </MenuSection>
  );
}

/**
 * Renders the user menu at the bottom of the sidebar. Part 1a has no sign-in, so it says so,
 * with a quiet preview line under it.
 *
 * @returns The menu and the preview line.
 */
export function UserMenu(): JSX.Element {
  const { t } = useTranslation();
  const title = useId();
  return (
    <div className="user-menu">
      <MenuTrigger>
        <Button className="user-menu-button">{t("shell.notSignedIn")}</Button>
        <Popover className="float" placement="top start">
          <p className="menu-title" id={title}>
            {t("shell.preferences")}
          </p>
          {/* Named by its title, not by the trigger: the trigger says who is signed in. */}
          <Menu className="menu" aria-labelledby={title}>
            <ChoiceSection
              field="theme"
              label={t("shell.theme")}
              options={THEMES}
              names="shell.themes"
            />
            <ChoiceSection
              field="density"
              label={t("shell.density")}
              options={DENSITIES}
              names="shell.densities"
            />
            <ChoiceSection
              field="language"
              label={t("shell.language")}
              options={LANGUAGES}
              names="shell.languages"
            />
          </Menu>
        </Popover>
      </MenuTrigger>
      <p className="user-menu-preview">{t("shell.preview")}</p>
    </div>
  );
}
