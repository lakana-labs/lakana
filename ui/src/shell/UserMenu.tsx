/**
 * The menu under the signed-in person's name: theme, density, language, the profile picture,
 * passkeys and sessions, and signing out.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useId, useState } from "react";
import {
  Button,
  Header,
  Menu,
  MenuItem,
  MenuSection,
  MenuTrigger,
  Popover,
} from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";

import { usePreferences } from "../preferences/PreferencesProvider";
import { Avatar } from "../profile/Avatar";
import type { Density, Language, Preferences, Theme } from "../preferences/preferences";
import { problemText } from "../sign-in/messages";
import { useSession } from "../sign-in/SessionProvider";
import { useMe } from "./me";

const PictureDialog = lazy(async () => {
  const { PictureDialog: loaded } = await import("../profile/PictureDialog");
  return { default: loaded };
});

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
 * Renders the user menu at the bottom of the sidebar. Its button always shows who is signed in,
 * with their picture or initials; the menu holds the personal preferences, one's profile picture,
 * passkeys and sessions, and signing out, here or on every device.
 *
 * @returns The menu, and what went wrong when signing out did not work.
 */
export function UserMenu(): JSX.Element {
  const { t } = useTranslation();
  const session = useSession();
  const title = useId();
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const me = useMe();
  const displayName = session.session?.display_name ?? null;
  const name = displayName ?? session.session?.name;
  const end = (ending: () => Promise<void>): void => {
    setError(null);
    ending().catch((failure: unknown) => {
      setError(t("shell.signOutFailed", { detail: problemText(t, failure) }));
    });
  };
  return (
    <div className="user-menu">
      <MenuTrigger>
        {/* The name is what shows; "Signed in as" is visually hidden around it. Not an
            aria-label: the initials are text, and a label must contain all that shows (2.5.3). */}
        <Button className="user-menu-button">
          <Avatar name={me.name} displayName={displayName} etag={me.picture} size={24} />
          <Trans
            t={t}
            i18nKey="shell.signedInAs"
            values={{ name }}
            components={{ hidden: <span className="visually-hidden" /> }}
          />
        </Button>
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
            <MenuSection className="menu-section">
              <Header className="menu-header">{t("shell.account")}</Header>
              <MenuItem
                className="menu-item"
                id="picture"
                onAction={() => {
                  setPicking(true);
                }}
              >
                <span className="menu-check" aria-hidden="true" />
                {t("profile.menuItem")}
              </MenuItem>
              <MenuItem className="menu-item" id="account" href="/account">
                <span className="menu-check" aria-hidden="true" />
                {t("account.title")}
              </MenuItem>
              <MenuItem
                className="menu-item"
                id="sign-out"
                onAction={() => {
                  end(session.signOut);
                }}
              >
                <span className="menu-check" aria-hidden="true" />
                {t("shell.signOut")}
              </MenuItem>
              <MenuItem
                className="menu-item"
                id="sign-out-everywhere"
                onAction={() => {
                  end(session.signOutEverywhere);
                }}
              >
                <span className="menu-check" aria-hidden="true" />
                {t("shell.signOutEverywhere")}
              </MenuItem>
            </MenuSection>
          </Menu>
        </Popover>
      </MenuTrigger>
      {picking && (
        <Suspense fallback={null}>
          <PictureDialog
            name={me.name}
            displayName={displayName}
            etag={me.picture}
            onClose={() => {
              setPicking(false);
            }}
          />
        </Suspense>
      )}
      {error !== null && (
        <p className="user-menu-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
