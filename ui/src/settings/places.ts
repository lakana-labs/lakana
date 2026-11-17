/**
 * The registry of Settings' places: where each lives, what it is called and who is shown it.
 *
 * @remarks
 * The list, the picker, the routes and `/settings/access`'s entry all read it. It imports
 * nothing of Lakana's but a type, so the pages (which import the frame) never form a cycle with it.
 * @packageDocumentation
 */
import { matchRoutes, type RouteObject } from "react-router";

import type { AccessOut } from "../api";

/** The id of a place. */
export type SettingsPlaceId =
  "status" | "repositories" | "access" | "people" | "sign-in" | "invites" | "recovery-links";

/** What the rules know about the person: a part of what `GET /api/v1/whoami` answers. */
export type SettingsViewer = Pick<AccessOut, "admin" | "noAccessYet">;

/** One place in Settings. */
export interface SettingsPlace {
  /** Its id. */
  readonly id: SettingsPlaceId;
  /** Its path under `/settings`, without slashes at either end (`"sign-in/invites"`). */
  readonly path: string;
  /** The i18n key of its name (`"settings.places.signIn"`). */
  readonly label: string;
  /** The place it is listed under, or null at the top. */
  readonly parent: SettingsPlaceId | null;
  /** A page, or a group: a plain label over its children, with no page of its own. */
  readonly kind: "page" | "group";
  /** Whether the page owns the paths under its own. */
  readonly below: boolean;
  /** Whether this person may see the place. */
  readonly visible: (viewer: SettingsViewer) => boolean;
}

/**
 * Every place, in list order.
 *
 * @remarks
 * The spec's full order is: Status, Environments, Repositories, Access (People, Groups, Grants,
 * Tokens, Secrets, Row filters & masks, Access file, What I may do), Sign-in (Invites, Recovery
 * links), Configuration. A new place is one entry here, at its place in that order, plus its
 * loader in `placePages.ts` (kept literal, `as const`, so that file can tell the pages from the
 * groups and refuse a page without a loader).
 */
export const SETTINGS_PLACES = [
  {
    id: "status",
    path: "status",
    label: "settings.places.status",
    parent: null,
    kind: "page",
    below: false,
    visible: () => true,
  },
  {
    id: "repositories",
    path: "repositories",
    label: "settings.places.repositories",
    parent: null,
    kind: "page",
    below: true,
    visible: (viewer) => viewer.admin,
  },
  {
    id: "access",
    path: "access",
    label: "settings.places.access",
    parent: null,
    kind: "group",
    below: false,
    visible: () => true,
  },
  {
    id: "people",
    path: "access/people",
    label: "settings.places.people",
    parent: "access",
    kind: "page",
    below: true,
    visible: (viewer) => viewer.admin,
  },
  {
    id: "sign-in",
    path: "sign-in",
    label: "settings.places.signIn",
    parent: null,
    kind: "page",
    below: false,
    visible: (viewer) => viewer.admin,
  },
  {
    id: "invites",
    path: "sign-in/invites",
    label: "settings.places.invites",
    parent: "sign-in",
    kind: "page",
    below: false,
    visible: (viewer) => viewer.admin,
  },
  {
    id: "recovery-links",
    path: "sign-in/recovery-links",
    label: "settings.places.recoveryLinks",
    parent: "sign-in",
    kind: "page",
    below: false,
    visible: (viewer) => viewer.admin,
  },
] as const satisfies readonly SettingsPlace[];

/**
 * The places this person is shown, in list order.
 *
 * @remarks
 * A page is listed when its rule holds and its parent, if a page, is listed. A group is listed
 * when one of its children is.
 * @param viewer - What the rules know about the person.
 * @param places - The registry; only a test passes another.
 * @returns The listed places.
 */
export function listedPlaces(
  viewer: SettingsViewer,
  places: readonly SettingsPlace[] = SETTINGS_PLACES,
): readonly SettingsPlace[] {
  const listed = (place: SettingsPlace): boolean => {
    if (place.kind === "group") {
      return places.some((child) => child.parent === place.id && listed(child));
    }
    const parent = places.find((candidate) => candidate.id === place.parent);
    return (
      place.visible(viewer) && (parent === undefined || parent.kind === "group" || listed(parent))
    );
  };
  return places.filter(listed);
}

/**
 * A place's route pattern under `/settings`: its path, and everything below it when the page
 * owns the paths under its own.
 *
 * @param place - The place.
 * @returns The pattern, as the Settings screen's routes and {@link placeAt} both use it.
 */
export function placePattern(place: SettingsPlace): string {
  return place.below ? `${place.path}/*` : place.path;
}

/**
 * The place an address belongs to, as the router matches it: each segment decoded, in any
 * letter case, a trailing slash ignored. Null when no place matches.
 *
 * @remarks
 * It marks the current place in the list. It never decides whether a place is shown: each
 * place's own route does that, so the guard cannot disagree with the router.
 * @param pathname - The address's path.
 * @param places - The registry; only a test passes another.
 * @returns The place, or null.
 */
export function placeAt(
  pathname: string,
  places: readonly SettingsPlace[] = SETTINGS_PLACES,
): SettingsPlace | null {
  const routes: RouteObject[] = [
    {
      path: "/settings",
      children: places.map((place) => ({ path: placePattern(place), handle: place })),
    },
  ];
  const handle: unknown = matchRoutes(routes, pathname)?.at(-1)?.route.handle;
  return (handle as SettingsPlace | undefined) ?? null;
}

/**
 * A place's address.
 *
 * @param place - The place.
 * @returns Its path from the root.
 */
export function placePath(place: SettingsPlace): string {
  return `/settings/${place.path}`;
}

/**
 * Where `/settings/<place>` leads: a page's own path; a group's first listed child's path; null
 * when nothing is listed.
 *
 * @param place - The place.
 * @param viewer - What the rules know about the person.
 * @param places - The registry; only a test passes another.
 * @returns The path, or null.
 */
export function entryPath(
  place: SettingsPlace,
  viewer: SettingsViewer,
  places: readonly SettingsPlace[] = SETTINGS_PLACES,
): string | null {
  if (place.kind === "page") return placePath(place);
  const child = listedPlaces(viewer, places).find((candidate) => candidate.parent === place.id);
  return child === undefined ? null : entryPath(child, viewer, places);
}
