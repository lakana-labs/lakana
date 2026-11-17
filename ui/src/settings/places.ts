/**
 * The registry of Settings' places: where each lives, what it is called and who is shown it.
 *
 * @remarks
 * The list, the picker, the route guard and `/settings/access`'s entry all read it. It imports
 * nothing but a type, so the pages (which import the frame) never form a cycle with it.
 * @packageDocumentation
 */
import type { AccessOut } from "../api";

/** The id of a place. */
export type SettingsPlaceId = "status" | "repositories" | "sign-in";

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
 * loader in `placePages.ts`.
 */
export const SETTINGS_PLACES: readonly SettingsPlace[] = [
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
    id: "sign-in",
    path: "sign-in",
    label: "settings.places.signIn",
    parent: null,
    kind: "page",
    below: false,
    visible: (viewer) => viewer.admin,
  },
];

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
 * The place an address belongs to: the longest registry path that is a whole-segment prefix of
 * what follows `/settings/`, ignoring a trailing slash. Null when there is none.
 *
 * @param pathname - The address's path.
 * @param places - The registry; only a test passes another.
 * @returns The place, or null.
 */
export function placeAt(
  pathname: string,
  places: readonly SettingsPlace[] = SETTINGS_PLACES,
): SettingsPlace | null {
  const prefix = "/settings/";
  if (!pathname.startsWith(prefix)) return null;
  const rest = pathname.slice(prefix.length).replace(/\/+$/, "");
  let found: SettingsPlace | null = null;
  for (const place of places) {
    const within = rest === place.path || rest.startsWith(`${place.path}/`);
    if (within && (found === null || place.path.length > found.path.length)) found = place;
  }
  return found;
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
