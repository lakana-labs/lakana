/**
 * The Settings area's screen, loaded on demand: the list of places beside the place the address
 * names. Every place's page loads on its own.
 *
 * @packageDocumentation
 */
import {
  type ComponentType,
  type JSX,
  type ReactNode,
  lazy,
  type LazyExoticComponent,
  type RefObject,
  Suspense,
  useEffect,
  useRef,
} from "react";
import {
  Navigate,
  NavigationType,
  Route,
  Routes,
  useLocation,
  useNavigationType,
} from "react-router";
import { useTranslation } from "react-i18next";

import { NotAvailable } from "../shell/NotAvailable";
import { NotFound } from "../shell/NotFound";
import { useMe } from "../shell/me";
import { isRedirect, REDIRECT } from "../shell/redirect";
import { GroupEntry } from "./GroupEntry";
import { type PagePlaceId, PLACE_PAGES } from "./placePages";
import { listedPlaces, placePattern, SETTINGS_PLACES, type SettingsPlace } from "./places";
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

/** What {@link FocusHeading} reads to decide, kept by {@link AreaScreen}. */
interface FocusTrack {
  /** Set once the person has moved from the address the area opened on. */
  moved: boolean;
  /** A count that rises with every address change and every key or pointer press. */
  step: number;
  /** The step of the last address change. */
  navigated: number;
  /** The step of the person's last key or pointer press. */
  acted: number;
}

/**
 * Moves the focus to the page's heading once a place that was loading has shown, after a
 * navigation inside Settings. The shell moves it when the address changes, but a place whose
 * chunk is still loading has no heading yet. Not on the first load, so the skip link stays the
 * first stop; and not when the person has pressed a key or a pointer since the address changed
 * (Tab, or Section opened again), so the focus stays where they put it.
 *
 * @param props - What the area tracked.
 * @param props.track - The moves and presses.
 * @returns Nothing to show.
 */
function FocusHeading({ track }: { readonly track: RefObject<FocusTrack> }): null {
  useEffect(() => {
    const { moved, navigated, acted } = track.current;
    if (moved && acted < navigated) {
      document.getElementById("main")?.querySelector("h1")?.focus();
    }
  }, [track]);
  return null;
}

/** Properties of {@link Guarded}. */
interface GuardedProps {
  /** The place whose route matched. */
  readonly place: SettingsPlace;
  /** The place's page, shown only when the person is shown the place. */
  readonly children: ReactNode;
}

/**
 * Shows a place's page only to a person who is shown the place; anyone else gets
 * {@link NotAvailable} with its name, so the page is never loaded and its routes never read.
 *
 * @remarks
 * It sits inside the place's own route, so the router alone decides which place an address is
 * (in any letter case, with any segment percent-encoded); no second reading of the address can
 * let a hidden place through.
 * @param props - The place and its page.
 * @param props.place - The place whose route matched.
 * @param props.children - The page.
 * @returns The page, or the permission state.
 */
function Guarded({ place, children }: GuardedProps): JSX.Element {
  const { t } = useTranslation();
  const { admin, noAccessYet } = useMe();
  return listedPlaces({ admin, noAccessYet }).includes(place) ? (
    <>{children}</>
  ) : (
    <NotAvailable place={t(place.label)} />
  );
}

/**
 * Renders the place the address names under `/settings`, inside the Settings layout;
 * `/settings` itself opens Status, where Home's "Details" goes, and a group's address
 * (`/settings/access`) its first listed place.
 *
 * @returns The place.
 */
export function AreaScreen(): JSX.Element {
  const location = useLocation();
  const { pathname } = location;
  const state: unknown = location.state;
  const opened = useRef(pathname);
  const navigationType = useNavigationType();
  // Arriving by a move (a click on Settings in the sidebar) already counts as one.
  const track = useRef<FocusTrack>({
    moved: navigationType === NavigationType.Push,
    step: 1,
    navigated: 1,
    acted: 0,
  });
  useEffect(() => {
    const current = track.current;
    current.step += 1;
    current.navigated = current.step;
    if (navigationType === NavigationType.Replace && isRedirect(state)) {
      // A redirect (/settings to Status, a group to its first place) settles where the visit
      // opened; it is not the person moving.
      opened.current = pathname;
    } else {
      current.moved ||= pathname !== opened.current;
    }
  }, [pathname, navigationType, state]);
  useEffect(() => {
    // A press is what moves the focus; the press that chose the place comes before its address.
    const pressed = (): void => {
      const current = track.current;
      current.step += 1;
      current.acted = current.step;
    };
    document.addEventListener("keydown", pressed, { capture: true });
    document.addEventListener("pointerdown", pressed, { capture: true });
    return () => {
      document.removeEventListener("keydown", pressed, { capture: true });
      document.removeEventListener("pointerdown", pressed, { capture: true });
    };
  }, []);
  return (
    <Routes>
      <Route element={<SettingsLayout />}>
        <Route index element={<Navigate to="status" replace state={REDIRECT} />} />
        {PAGE_PLACES.map((place) => {
          const Page = PAGES[place.id];
          return (
            <Route
              key={place.id}
              path={placePattern(place)}
              element={
                <Guarded key={place.id} place={place}>
                  {/* Keyed by place: leaving a place unmounts it at once, even while the next one loads. */}
                  <Suspense fallback={null}>
                    <Page />
                    <FocusHeading track={track} />
                  </Suspense>
                </Guarded>
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
