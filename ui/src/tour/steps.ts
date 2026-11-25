/**
 * The steps of the first-sign-in tour: the places of the sidebar the person sees, then Search and
 * Help.
 *
 * @packageDocumentation
 */
import type { AreaId, ShippedArea } from "../shell/areas";

/** One step of the tour; its words are `tour.steps.<id>` in `en.json`. */
export type TourStepId =
  "home" | "catalog" | "queries" | "jobs" | "connections" | "settings" | "search" | "help";

/**
 * The guide each step links to, as a path on the documentation site (`DOCS_URL`): the how-to
 * for its place, at the heading about the web interface where the page has one. Help, the last
 * step, links to the tutorial, the how-to guides and the overview instead. `steps.test.ts`
 * checks that each page and heading exists under `docs/site`.
 */
export const TOUR_GUIDES: Readonly<Record<Exclude<TourStepId, "help">, string>> = {
  home: "how-to/start-in-the-web-interface/#the-boxes-top-to-bottom",
  catalog: "how-to/find-a-table/#in-the-web-interface",
  queries: "how-to/start-in-the-web-interface/#queries",
  jobs: "how-to/find-a-run/#find-what-failed-in-every-environment",
  connections: "how-to/load-data-from-a-source/#1-create-the-connection",
  settings: "how-to/instance-health/",
  search: "how-to/start-in-the-web-interface/#keyboard-shortcuts",
};

/** The areas the tour has a step for. */
const AREA_STEPS: ReadonlySet<AreaId> = new Set<AreaId>([
  "home",
  "catalog",
  "queries",
  "jobs",
  "connections",
  "settings",
]);

/**
 * Whether the tour has a step for an area.
 *
 * @param id - The area.
 * @returns True when it has.
 */
function isStep(id: AreaId): id is AreaId & TourStepId {
  return AREA_STEPS.has(id);
}

/**
 * The tour's steps, in sidebar order: a place the person cannot use is left out, as it is in the
 * sidebar.
 *
 * @param areas - The areas the person sees, in sidebar order.
 * @returns The steps.
 */
export function tourSteps(areas: readonly ShippedArea[]): readonly TourStepId[] {
  return [...areas.map((area) => area.id).filter(isStep), "search", "help"];
}

/**
 * The selector of the place a step is about.
 *
 * @param id - The step.
 * @returns The selector; the sidebar marks each place with `data-tour`.
 */
export function tourTarget(id: TourStepId): string {
  return `[data-tour="${id}"]`;
}
