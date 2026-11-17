import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import {
  SETTINGS_PLACES,
  type SettingsPlace,
  entryPath,
  listedPlaces,
  placeAt,
  placePath,
} from "./places";

const i18n = initI18n();
const ADMIN = { admin: true, noAccessYet: false };
const KIM = { admin: false, noAccessYet: false };
const NEW = { admin: false, noAccessYet: true };

const page = (id: string, parent: string | null, admin: boolean): SettingsPlace =>
  ({
    id,
    path: parent === null ? id : `${parent}/${id}`,
    label: id,
    parent,
    kind: "page",
    below: false,
    visible: (viewer) => viewer.admin || !admin,
  }) as SettingsPlace;
const group = { ...page("grp", null, false), kind: "group" } as SettingsPlace;
const TREE: readonly SettingsPlace[] = [
  page("top", null, true),
  page("kid", "top", false),
  group,
  page("inside", "grp", true),
  { ...group, id: "empty" as SettingsPlace["id"], path: "empty" },
];

describe("places", () => {
  it("lists every built place for an administrator, in order", () => {
    expect(listedPlaces(ADMIN).map((place) => place.id)).toEqual([
      "status",
      "repositories",
      "access",
      "people",
      "sign-in",
      "invites",
      "recovery-links",
    ]);
  });

  it("lists only Status for a person who is not an administrator", () => {
    expect(listedPlaces(KIM).map((place) => place.id)).toEqual(["status"]);
    expect(listedPlaces(NEW).map((place) => place.id)).toEqual(["status"]);
  });

  it("lists a child only with its parent, and a group only with a listed child", () => {
    expect(listedPlaces(ADMIN, TREE).map((place) => place.id)).toEqual([
      "top",
      "kid",
      "grp",
      "inside",
    ]);
    expect(listedPlaces(KIM, TREE)).toEqual([]);
  });

  it("finds the place of an address", () => {
    expect(placeAt("/settings/repositories/new")?.id).toBe("repositories");
    expect(placeAt("/settings/sign-in/")?.id).toBe("sign-in");
    expect(placeAt("/settings/access/people/maria")?.id).toBe("people");
    expect(placeAt("/settings/sign-in/invites")?.id).toBe("invites");
    expect(placeAt("/settings/access/")?.id).toBe("access");
    expect(placeAt("/settings/statusx")).toBeNull();
    expect(placeAt("/settings")).toBeNull();
    expect(placeAt("/other/status")).toBeNull();
  });

  it("prefers the longest path", () => {
    expect(placeAt("/settings/top/kid/x", TREE)?.id).toBe("kid");
    expect(placeAt("/settings/top", TREE)?.id).toBe("top");
  });

  it("gives each page its own path as its entry", () => {
    const place = placeAt("/settings/sign-in");
    if (place === null) throw new Error("no place");
    expect(placePath(place)).toBe("/settings/sign-in");
    expect(entryPath(place, ADMIN)).toBe("/settings/sign-in");
  });

  it("enters a group at its first listed child, or nowhere", () => {
    expect(entryPath(group, ADMIN, TREE)).toBe("/settings/grp/inside");
    expect(entryPath(group, KIM, TREE)).toBeNull();
  });

  it("enters Access at People for an administrator, and nowhere for anyone else", () => {
    const access = placeAt("/settings/access");
    if (access === null) throw new Error("no place");
    expect(entryPath(access, ADMIN)).toBe("/settings/access/people");
    expect(entryPath(access, KIM)).toBeNull();
  });

  it("leaves Access open to everyone, so only its places decide whether it is listed", () => {
    const access = placeAt("/settings/access");
    expect(access?.visible(KIM)).toBe(true);
    expect(listedPlaces(KIM)).not.toContain(access);
  });

  it("names every place in the frame's strings", async () => {
    await import("./settingsStrings");
    for (const place of SETTINGS_PLACES) expect(i18n.t(place.label)).not.toBe(place.label);
    expect(i18n.t("settings.places.label")).toBe("Settings places");
    expect(i18n.t("settings.places.picker")).toBe("Section");
  });
});
