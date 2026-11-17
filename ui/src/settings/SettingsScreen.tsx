/**
 * The Settings area's screen, loaded on demand: the list of places beside the place the address
 * names. Every place's page loads on its own.
 *
 * @packageDocumentation
 */
import {
  type ComponentType,
  type JSX,
  lazy,
  type LazyExoticComponent,
  type RefObject,
  Suspense,
  useEffect,
  useRef,
} from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";

import { NotFound } from "../shell/NotFound";
import { GroupEntry } from "./GroupEntry";
import { type PagePlaceId, PLACE_PAGES } from "./placePages";
import { SETTINGS_PLACES, type SettingsPlace } from "./places";
import { SettingsLayout } from "./SettingsLayout";

// Each page is made lazy once, here, so a render never makes a new component (and loses its state).
const PAGES = Object.fromEntries(
  Object.entries(PLACE_PAGES).map(([id, load]) => [
    id,
    lazy(async () => ({ default: await load() })),
  ]),
) as Record<PagePlaceId, LazyExoticComponent<ComponentType>>;

/**
 * Whether a place has a page of its own, and so a loader in {@link PLACE_PAGES}.
 *
 * @param place - The place.
 * @returns True for a page, false for a group.
 */
function hasPage(place: SettingsPlace): place is SettingsPlace & { readonly id: PagePlaceId } {
  return place.kind === "page";
}

/** The registry in its general type, so each place's kind is read, not assumed. */
const REGISTRY: readonly SettingsPlace[] = SETTINGS_PLACES;

/** The places with a page of their own, in registry order. */
const PAGE_PLACES = REGISTRY.filter(hasPage);

/** The groups, which have no page of their own: their address opens their first listed place. */
const GROUPS = REGISTRY.filter((place) => !hasPage(place));

/**
 * Moves the focus to the page's heading once a place that was loading has shown, after a
 * navigation inside Settings. The shell moves it when the address changes, but a place whose
 * chunk is still loading has no heading yet; not on the first load, so the skip link stays the
 * first stop.
 *
 * @param props - Whether the person has moved from the address the area opened on.
 * @param props.moved - Set once the address has changed.
 * @returns Nothing to show.
 */
function FocusHeading({ moved }: { readonly moved: RefObject<boolean> }): null {
  useEffect(() => {
    if (moved.current) {
      document.getElementById("main")?.querySelector("h1")?.focus();
    }
  }, [moved]);
  return null;
}

/**
 * Renders the place the address names under `/settings`, inside the Settings layout;
 * `/settings` itself opens Status, where Home's "Details" goes, and a group's address
 * (`/settings/access`) its first listed place.
 *
 * @returns The place.
 */
export function AreaScreen(): JSX.Element {
  const { pathname } = useLocation();
  const opened = useRef(pathname);
  const moved = useRef(false);
  useEffect(() => {
    moved.current = pathname !== opened.current;
  }, [pathname]);
  return (
    <Routes>
      <Route element={<SettingsLayout />}>
        <Route index element={<Navigate to="status" replace />} />
        {PAGE_PLACES.map((place) => {
          const Page = PAGES[place.id];
          return (
            <Route
              key={place.id}
              path={place.below ? `${place.path}/*` : place.path}
              element={
                // Keyed by place: leaving a place unmounts it at once, even while the next one loads.
                <Suspense key={place.id} fallback={null}>
                  <Page />
                  <FocusHeading moved={moved} />
                </Suspense>
              }
            />
          );
        })}
        {GROUPS.map((group) => (
          <Route key={group.id} path={group.path} element={<GroupEntry group={group} />} />
        ))}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
