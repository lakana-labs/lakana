/**
 * Settings › Configuration's rows: the server's settings grouped as the settings reference
 * groups them, found by name, and each value in words.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { ConfigurationSettingOut } from "../api/configuration";
import "./configurationStrings";

/** The prefix every setting of the server starts with. */
const PREFIX = "LAKANA_";

/** The settings of one group, in the server's order. */
export interface SettingGroup {
  /** The group's key: its prefix, or `"general"` for the settings without a group. */
  readonly id: string;
  /** The group's prefix, such as `LAKANA_RUN__*`; null for General. */
  readonly prefix: string | null;
  /** The group's settings, in the server's order. */
  readonly settings: readonly ConfigurationSettingOut[];
}

/**
 * The group of a setting, as the settings reference names it: the part after `LAKANA_` up to
 * the first `__`.
 *
 * @param name - The environment variable, such as `LAKANA_RUN__LEASE_SECONDS`.
 * @returns The group, such as `LAKANA_RUN__*`; null for a setting without a group.
 */
export function settingGroupOf(name: string): string | null {
  const group = /^LAKANA_(.+?)__/u.exec(name)?.[1];
  return group === undefined ? null : `${PREFIX}${group}__*`;
}

/**
 * Groups the settings: each group where its first setting is, and the settings in the server's
 * order within it.
 *
 * @param settings - The settings, in the server's order.
 * @returns The groups.
 */
export function groupSettings(
  settings: readonly ConfigurationSettingOut[],
): readonly SettingGroup[] {
  const groups = new Map<string, { prefix: string | null; settings: ConfigurationSettingOut[] }>();
  for (const setting of settings) {
    const prefix = settingGroupOf(setting.name);
    const id = prefix ?? "general";
    const group = groups.get(id);
    if (group === undefined) {
      groups.set(id, { prefix, settings: [setting] });
    } else {
      group.settings.push(setting);
    }
  }
  return [...groups].map(([id, group]) => ({ id, ...group }));
}

/**
 * Whether a setting's name holds the words, in any case; a leading `LAKANA_` in the words is
 * optional, so `run__lease` and `LAKANA_RUN__LEASE` find the same setting.
 *
 * @param setting - The setting.
 * @param words - What the person typed; blank matches every setting.
 * @returns True when the name holds the words.
 */
export function matchesSetting(setting: ConfigurationSettingOut, words: string): boolean {
  const typed = words.trim().toUpperCase();
  const wanted = typed.startsWith(PREFIX) ? typed.slice(PREFIX.length) : typed;
  return setting.name.toUpperCase().includes(wanted);
}

/** A setting's value in words, and whether it shows in the muted colour. */
export interface SettingValueText {
  /** The words. */
  readonly text: string;
  /** True for a setting that holds no value. */
  readonly muted: boolean;
}

/**
 * A setting's value as `.env` takes it. A secret says only whether it is set, from `has_value`
 * alone: its `value` is never read, so even a server that sent one shows nothing of it.
 *
 * @param t - The translation function.
 * @param setting - The setting.
 * @returns The words, muted for a setting that holds no value.
 */
export function settingValueText(t: TFunction, setting: ConfigurationSettingOut): SettingValueText {
  if (setting.secret) {
    return {
      text: setting.has_value
        ? t("settings.configuration.secretSet")
        : t("settings.configuration.secretNotSet"),
      muted: false,
    };
  }
  if (!setting.has_value) {
    return { text: t("settings.configuration.notSet"), muted: true };
  }
  const value = setting.value;
  if (typeof value === "string") {
    return { text: value, muted: false };
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return { text: String(value), muted: false };
  }
  return { text: JSON.stringify(value), muted: false };
}
