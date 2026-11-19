import i18next from "i18next";
import { describe, expect, it } from "vitest";

import type { ConfigurationSettingOut } from "../api/configuration";
import {
  groupSettings,
  matchesSetting,
  settingGroupOf,
  settingValueText,
} from "./configurationRows";

/**
 * A setting as the server answers it.
 *
 * @param name - The environment variable.
 * @param over - What differs from a plain value that was set.
 * @returns The setting.
 */
function aSetting(
  name: string,
  over: Partial<ConfigurationSettingOut> = {},
): ConfigurationSettingOut {
  return { name, value: "x", secret: false, has_value: true, source: "set", ...over };
}

const t = i18next.t.bind(i18next);

describe("configurationRows", () => {
  it("names a setting's group as the settings reference does", () => {
    expect(settingGroupOf("LAKANA_RUN__LEASE_SECONDS")).toBe("LAKANA_RUN__*");
    expect(settingGroupOf("LAKANA_CI_OIDC__PROXY")).toBe("LAKANA_CI_OIDC__*");
    expect(settingGroupOf("LAKANA_BOOTSTRAP_PAT_KEY_ID")).toBeNull();
    expect(settingGroupOf("LAKANA_HOST")).toBeNull();
    expect(settingGroupOf("OTHER__HOST")).toBeNull();
  });

  it("groups as the settings reference does", () => {
    const settings = [
      "LAKANA_DATABASE__URL",
      "LAKANA_DATABASE__ECHO",
      "LAKANA_BOOTSTRAP_ADMIN",
      "LAKANA_SECRETS__MASTER_KEY",
      "LAKANA_HOST",
    ].map((name) => aSetting(name));

    const groups = groupSettings(settings);

    expect(groups.map((group) => [group.id, group.prefix])).toEqual([
      ["LAKANA_DATABASE__*", "LAKANA_DATABASE__*"],
      ["general", null],
      ["LAKANA_SECRETS__*", "LAKANA_SECRETS__*"],
    ]);
    expect(groups.map((group) => group.settings.map((setting) => setting.name))).toEqual([
      ["LAKANA_DATABASE__URL", "LAKANA_DATABASE__ECHO"],
      ["LAKANA_BOOTSTRAP_ADMIN", "LAKANA_HOST"],
      ["LAKANA_SECRETS__MASTER_KEY"],
    ]);
    expect(groupSettings([])).toEqual([]);
  });

  it("never reads a secret's value", () => {
    expect(
      settingValueText(
        t,
        aSetting("LAKANA_SECRETS__MASTER_KEY", { secret: true, value: "mk-sentinel" }),
      ),
    ).toEqual({ text: "secret, set", muted: false });
    expect(
      settingValueText(
        t,
        aSetting("LAKANA_SECRETS__MASTER_KEY", { secret: true, has_value: false, value: null }),
      ),
    ).toEqual({ text: "secret, not set", muted: false });
    const trap = {
      name: "LAKANA_RUN__INDEX_URL",
      secret: true,
      has_value: true,
      source: "set",
      get value(): never {
        throw new Error("A secret's value was read.");
      },
    } as ConfigurationSettingOut;
    expect(settingValueText(t, trap).text).toBe("secret, set");
    const poisoned = aSetting("LAKANA_RUN__HTTPS_PROXY", {
      secret: true,
      value: new Proxy(
        {},
        {
          get: () => {
            throw new Error("A secret's value was touched.");
          },
        },
      ) as never,
    });
    expect(settingValueText(t, poisoned).text).toBe("secret, set");
  });

  it.each<[string, ConfigurationSettingOut["value"], string]>([
    ["a port", 8000, "8000"],
    ["a large number, without grouping", 268435456, "268435456"],
    ["a fraction", 0.5, "0.5"],
    ["false", false, "false"],
    ["true", true, "true"],
    ["a list", ["RS256"], '["RS256"]'],
    ["an empty list", [], "[]"],
    ["an object", { raw: "/srv/raw" }, '{"raw":"/srv/raw"}'],
    ["a string", "1GB", "1GB"],
  ])("writes each value as .env takes it: %s", (_label, value, text) => {
    expect(settingValueText(t, aSetting("LAKANA_X", { value }))).toEqual({ text, muted: false });
  });

  it("says not set, muted, for a setting without a value", () => {
    expect(
      settingValueText(t, aSetting("LAKANA_RUN__CA_BUNDLE", { value: null, has_value: false })),
    ).toEqual({ text: "not set", muted: true });
  });

  it.each(["lease", "LEASE", "run__lease", "LAKANA_RUN", "lakana_run__lease", "  ", ""])(
    "matches any part of the name: %j",
    (words) => {
      expect(matchesSetting(aSetting("LAKANA_RUN__LEASE_SECONDS"), words)).toBe(true);
    },
  );

  it.each(["zzz", "LAKANA_ZZZ", "seconds lease"])("does not match %j", (words) => {
    expect(matchesSetting(aSetting("LAKANA_RUN__LEASE_SECONDS"), words)).toBe(false);
  });

  it("matches the prefix itself and words around it", () => {
    expect(matchesSetting(aSetting("LAKANA_HOST"), "lakana_")).toBe(true);
    expect(matchesSetting(aSetting("LAKANA_HOST"), " host ")).toBe(true);
    expect(matchesSetting(aSetting("LAKANA_HOST"), "LAKANA_HOST")).toBe(true);
  });
});
