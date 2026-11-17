/**
 * The Settings area's screen, loaded on demand: the list of places beside the place the address
 * names. Every place's page loads on its own.
 *
 * @packageDocumentation
 */
import { type ComponentType, type JSX, lazy, type LazyExoticComponent, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router";

import { NotFound } from "../shell/NotFound";
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

/**
 * Renders the place the address names under `/settings`, inside the Settings layout;
 * `/settings` itself opens Status, where Home's "Details" goes.
 *
 * @returns The place.
 */
export function AreaScreen(): JSX.Element {
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
                </Suspense>
              }
            />
          );
        })}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
