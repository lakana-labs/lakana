/**
 * Settings › Configuration: every setting of the running server, read-only, found by name.
 *
 * @remarks
 * A secret shows only whether it is set, from `has_value` alone. The answer is kept in this
 * component's state only: never polled, never stored.
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Button, Input, Label, SearchField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { type ConfigurationSettingOut, getConfiguration } from "../api/configuration";
import { countWords, ListToolbar } from "../shell/ListToolbar";
import { DOCS_URL } from "../shell/docs";
import { useResource } from "../shell/useResource";
import { VisibleText } from "../shell/VisibleText";
import { groupSettings, matchesSetting, settingValueText } from "./configurationRows";
import "./configurationStrings";
import { DetailRead } from "./DetailRead";
import { SettingsFrame } from "./SettingsFrame";
import "./settingsStrings";

/** Where the settings reference lives. */
const REFERENCE_URL = `${DOCS_URL}reference/settings/`;

/** Properties of {@link SettingsList}. */
interface SettingsListProps {
  /** The server's settings, in its order. */
  readonly settings: readonly ConfigurationSettingOut[];
}

/**
 * The find field, its words in the address as `?q=` (replaced, not pushed), and the settings
 * that match: one table, a group row above each group, or the no-match sentence with Clear
 * search.
 *
 * @param props - The settings.
 * @param props.settings - The server's settings.
 * @returns The field and the list.
 */
function SettingsList({ settings }: SettingsListProps): JSX.Element {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [typed, setTyped] = useState(params.get("q") ?? "");
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (typed === (params.get("q") ?? "")) return;
    setParams(typed === "" ? {} : { q: typed }, { replace: true });
  }, [typed, params, setParams]);
  const groups = groupSettings(settings.filter((setting) => matchesSetting(setting, typed)));
  return (
    <>
      <ListToolbar
        count={countWords(
          t,
          "settings.configuration",
          groups.reduce((sum, group) => sum + group.settings.length, 0),
        )}
        search={
          <SearchField className="field" value={typed} onChange={setTyped}>
            <Label className="visually-hidden">{t("settings.configuration.find")}</Label>
            <Input ref={field} placeholder={t("settings.configuration.find")} />
          </SearchField>
        }
      />
      {groups.length === 0 ? (
        <div className="empty">
          <p>{t("settings.configuration.noMatch", { words: typed.trim() })}</p>
          <Button
            className="button"
            onPress={() => {
              setTyped("");
              field.current?.focus();
            }}
          >
            {t("settings.configuration.clear")}
          </Button>
        </div>
      ) : (
        <div
          className="list-scroll configuration-scroll"
          role="region"
          aria-label={t("settings.configuration.region")}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          <table className="list list-static">
            <thead>
              <tr>
                <th scope="col">{t("settings.configuration.setting")}</th>
                <th scope="col">{t("settings.configuration.value")}</th>
                <th scope="col">{t("settings.configuration.source")}</th>
              </tr>
            </thead>
            {groups.map((group) => (
              <tbody key={group.id}>
                <tr>
                  <th scope="rowgroup" colSpan={3} className="setting-group">
                    {group.prefix === null ? (
                      t("settings.configuration.general")
                    ) : (
                      <span className="setting-prefix">
                        <VisibleText text={group.prefix} />
                      </span>
                    )}
                  </th>
                </tr>
                {group.settings.map((setting) => {
                  const value = settingValueText(t, setting);
                  return (
                    <tr key={setting.name}>
                      <td>
                        <span className="row-name setting-name">
                          <VisibleText text={setting.name} />
                        </span>
                      </td>
                      <td className="setting-value">
                        <span className={value.muted ? "setting-unset" : undefined}>
                          <VisibleText text={value.text} />
                        </span>
                      </td>
                      <td>
                        {setting.source === "set"
                          ? t("settings.configuration.sourceSet")
                          : t("settings.configuration.sourceDefault")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            ))}
          </table>
        </div>
      )}
    </>
  );
}

/**
 * Renders Settings › Configuration: what the page shows, where values change, the find field
 * and the list. A failed read offers Try again and puts the focus on the heading; a refusal
 * (the role taken away while the page was open) shows the server's sentence in the list's place.
 *
 * @returns The place.
 */
export function ConfigurationPage(): JSX.Element {
  const { t } = useTranslation();
  const read = useResource(getConfiguration);
  const heading = useRef<HTMLHeadingElement>(null);
  return (
    <SettingsFrame title={t("settings.places.configuration")} headingRef={heading}>
      <p className="page-purpose">{t("settings.configuration.purpose")}</p>
      <p>{t("settings.configuration.restart")}</p>
      <p>{t("settings.configuration.workers")}</p>
      <p>
        <a href={REFERENCE_URL} target="_blank" rel="noopener noreferrer">
          {t("settings.configuration.reference")}
        </a>
      </p>
      {read.failure?.status === 403 ? (
        <p className="step-error" role="alert">
          {read.error}
        </p>
      ) : (
        <DetailRead read={read} heading={heading} missing={null}>
          {(settings) => <SettingsList settings={settings} />}
        </DetailRead>
      )}
    </SettingsFrame>
  );
}
