import { describe, expect, it } from "vitest";

import { initI18n } from "../i18n/i18n";
import {
  SETTINGS_PLACES,
  type SettingsPlace,
  entryPath,
  listedPlaces,
  placeAt,
  placePath,
  mayCreateEnvironments,
  mayManageRepositories,
  managesEnvironment,
  placeState,
} from "./places";

const i18n = initI18n();
/** What every viewer below knows, unless it says otherwise: nothing read, nothing allowed. */
const NOTHING = {
  noAccessYet: false,
  secretEnvironments: [],
  readsEnvironments: false,
  instanceActions: new Set<string>(),
  managedEnvironments: [],
};
const ADMIN = {
  ...NOTHING,
  admin: true,
  managePolicy: true,
  readsEnvironments: true,
  instanceActions: new Set(["MANAGE_ENVIRONMENT", "MANAGE_REPOSITORY"]),
};
const KIM = { ...NOTHING, admin: false, managePolicy: false };
const NEW = { ...NOTHING, admin: false, noAccessYet: true, managePolicy: false };
/** A person who manages row filters and masks and is no administrator. */
const PAT = { ...NOTHING, admin: false, managePolicy: true };

/**
 * A place of the registry, by id.
 *
 * @param id - Its id.
 * @returns The place.
 */
const real = (id: string): SettingsPlace => {
  const found = SETTINGS_PLACES.find((place) => place.id === id);
  if (found === undefined) throw new Error(`no place ${id}`);
  return found;
};

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
    expect(
      listedPlaces({ ...ADMIN, secretEnvironments: ["dev"] }).map((place) => place.id),
    ).toEqual([
      "status",
      "environments",
      "repositories",
      "ci-trust",
      "access",
      "people",
      "groups",
      "grants",
      "tokens",
      "secrets",
      "filters-and-masks",
      "access-file",
      "what-i-may-do",
      "sign-in",
      "invites",
      "recovery-links",
      "configuration",
    ]);
  });

  it("lists CI trust to administrators only, with the rules' pages below it", () => {
    expect(listedPlaces(KIM).map((place) => place.id)).not.toContain("ci-trust");
    expect(listedPlaces({ ...KIM, readsEnvironments: true }).map((p) => p.id)).not.toContain(
      "ci-trust",
    );
    expect(placeAt("/settings/ci-trust")?.id).toBe("ci-trust");
    expect(placeAt("/settings/ci-trust/explain")?.id).toBe("ci-trust");
    expect(placeAt("/settings/ci-trust/7b0c7a52")?.id).toBe("ci-trust");
  });

  it("lists Configuration to administrators only", () => {
    expect(
      listedPlaces(ADMIN)
        .map((place) => place.id)
        .slice(-4),
    ).toEqual(["sign-in", "invites", "recovery-links", "configuration"]);
    expect(listedPlaces(KIM).map((place) => place.id)).not.toContain("configuration");
    expect(placeAt("/settings/configuration")?.id).toBe("configuration");
  });

  it("lists Row filters & masks to a policy manager, and Access file to administrators only", () => {
    expect(listedPlaces(PAT).map((place) => place.id)).toEqual([
      "status",
      "access",
      "filters-and-masks",
      "what-i-may-do",
    ]);
    expect(listedPlaces(KIM).map((place) => place.id)).not.toContain("filters-and-masks");
    expect(placeState(real("filters-and-masks"), { ...PAT, managePolicy: null })).toBe("pending");
    expect(placeState(real("access-file"), PAT)).toBe("hidden");
    expect(placeState(real("access-file"), ADMIN)).toBe("listed");
  });

  it("a reader is listed Repositories, a person who reads nothing is not", () => {
    const ids = (viewer: Parameters<typeof listedPlaces>[0]): readonly string[] =>
      listedPlaces(viewer).map((place) => place.id);
    expect(ids({ ...KIM, readsEnvironments: true })).toContain("repositories");
    expect(ids(KIM)).not.toContain("repositories");
    expect(ids({ ...KIM, instanceActions: new Set(["MANAGE_REPOSITORY"]) })).toContain(
      "repositories",
    );
    expect(placeState(real("repositories"), { ...KIM, readsEnvironments: null })).toBe("pending");
    expect(placeState(real("repositories"), { ...KIM, instanceActions: null })).toBe("pending");
  });

  it("a reader or a creator is listed Environments, a person with neither is not", () => {
    const ids = (viewer: Parameters<typeof listedPlaces>[0]): readonly string[] =>
      listedPlaces(viewer).map((place) => place.id);
    expect(ids({ ...KIM, readsEnvironments: true })).toEqual([
      "status",
      "environments",
      "repositories",
      "access",
      "what-i-may-do",
    ]);
    expect(ids({ ...KIM, instanceActions: new Set(["MANAGE_ENVIRONMENT"]) })).toContain(
      "environments",
    );
    expect(ids(KIM)).not.toContain("environments");
    expect(placeState(real("environments"), { ...KIM, readsEnvironments: null })).toBe("pending");
    expect(placeState(real("environments"), { ...KIM, instanceActions: null })).toBe("pending");
  });

  it("says who creates environments and who manages one, and nothing before it is read", () => {
    expect(mayCreateEnvironments(ADMIN)).toBe(true);
    expect(mayCreateEnvironments(KIM)).toBe(false);
    expect(mayCreateEnvironments({ ...KIM, instanceActions: null })).toBeNull();
    const mara = { ...KIM, managedEnvironments: ["dev"] };
    expect(managesEnvironment(mara, "dev")).toBe(true);
    expect(managesEnvironment(mara, "prod")).toBe(false);
    expect(managesEnvironment({ ...KIM, managedEnvironments: null }, "dev")).toBe(false);
    expect(managesEnvironment({ ...ADMIN, managedEnvironments: [] }, "staging")).toBe(true);
  });

  it("says who manages repositories, and nothing before it is read", () => {
    expect(mayManageRepositories(ADMIN)).toBe(true);
    expect(mayManageRepositories({ ...KIM, readsEnvironments: true })).toBe(false);
    expect(mayManageRepositories({ ...KIM, instanceActions: null })).toBeNull();
  });

  it("lists Status, Access and What I may do for a person who is not an administrator", () => {
    const shown = ["status", "access", "what-i-may-do"];
    expect(listedPlaces(KIM).map((place) => place.id)).toEqual(shown);
    expect(listedPlaces(NEW).map((place) => place.id)).toEqual(shown);
  });

  it("lists Secrets to whoever manages secrets in an environment", () => {
    expect(listedPlaces({ ...KIM, secretEnvironments: ["dev"] }).map((place) => place.id)).toEqual([
      "status",
      "access",
      "secrets",
      "what-i-may-do",
    ]);
    expect(listedPlaces(KIM).map((place) => place.id)).toEqual([
      "status",
      "access",
      "what-i-may-do",
    ]);
    expect(listedPlaces(ADMIN).map((place) => place.id)).not.toContain("secrets");
  });

  it("holds a place whose rule is not known yet as pending, and lists it nowhere", () => {
    const pending = { ...KIM, secretEnvironments: null };
    expect(placeState(real("secrets"), pending)).toBe("pending");
    expect(placeState(real("secrets"), KIM)).toBe("hidden");
    expect(placeState(real("tokens"), ADMIN)).toBe("listed");
    expect(listedPlaces(pending).map((place) => place.id)).not.toContain("secrets");
    const unknown = { ...page("later", null, false), visible: () => null } as SettingsPlace;
    const tree = [unknown, page("below", "later", false), group, page("inside", "grp", false)];
    expect(placeState(tree[1] as SettingsPlace, ADMIN, tree)).toBe("pending");
    const waiting = { ...group, id: "wait" as SettingsPlace["id"] };
    const nested = [waiting, { ...unknown, parent: "wait" as SettingsPlace["parent"] }];
    expect(placeState(waiting, ADMIN, nested)).toBe("pending");
    expect(placeState(group, ADMIN, tree)).toBe("listed");
    expect(placeState({ ...group, id: "none" as SettingsPlace["id"] }, ADMIN, tree)).toBe("hidden");
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
    expect(placeAt("/Settings/Sign-in/Invites")?.id).toBe("invites");
    expect(placeAt("/settings/statusx")).toBeNull();
    expect(placeAt("/settings")).toBeNull();
    expect(placeAt("/other/status")).toBeNull();
  });

  it("matches as the router does: the most specific route, nothing below a page that owns none", () => {
    expect(placeAt("/settings/top/kid", TREE)?.id).toBe("kid");
    expect(placeAt("/settings/top", TREE)?.id).toBe("top");
    expect(placeAt("/settings/top/kid/x", TREE)).toBeNull();
    expect(placeAt("/settings/top/x", TREE)).toBeNull();
  });

  it("reads each segment decoded, as the router does", () => {
    expect(placeAt("/settings/%73ign-in/invites")?.id).toBe("invites");
    expect(placeAt("/settings/%61ccess/people")?.id).toBe("people");
    expect(placeAt("/Settings/Access/%50eople/maria")?.id).toBe("people");
    expect(placeAt("/settings/sign-in%2Finvites")).toBeNull();
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

  it("enters Access at People for an administrator, and at What I may do for anyone else", () => {
    const access = placeAt("/settings/access");
    if (access === null) throw new Error("no place");
    expect(entryPath(access, ADMIN)).toBe("/settings/access/people");
    expect(entryPath(access, KIM)).toBe("/settings/access/what-i-may-do");
    expect(entryPath(access, { ...KIM, secretEnvironments: ["dev"] })).toBe(
      "/settings/access/secrets",
    );
    expect(entryPath(access, PAT)).toBe("/settings/access/filters-and-masks");
  });

  it("leaves Access open to everyone, so only its places decide whether it is listed", () => {
    const access = placeAt("/settings/access");
    expect(access?.visible(KIM)).toBe(true);
    expect(listedPlaces(KIM)).toContain(access);
  });

  it("names every place in the frame's strings", async () => {
    await import("./settingsStrings");
    for (const place of SETTINGS_PLACES) expect(i18n.t(place.label)).not.toBe(place.label);
    expect(i18n.t("settings.places.label")).toBe("Settings places");
    expect(i18n.t("settings.places.picker")).toBe("Section");
  });
});
