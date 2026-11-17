/**
 * The page of each place in Settings, loaded on demand.
 *
 * @remarks
 * Its own module, apart from the registry: the pages import the frame, and a registry that
 * imported them would form a cycle. Keyed by the page places' ids, so a page place without a
 * loader does not compile.
 * @packageDocumentation
 */
import type { ComponentType } from "react";

import type { SETTINGS_PLACES } from "./places";

/** The id of a place that has a page of its own (not a group). */
export type PagePlaceId = Extract<(typeof SETTINGS_PLACES)[number], { kind: "page" }>["id"];

/** Each page place's loader: it imports the page's chunk and hands back its component. */
export const PLACE_PAGES: Record<PagePlaceId, () => Promise<ComponentType>> = {
  /**
   * Status: the placeholder until part 8.
   *
   * @returns The page.
   */
  status: async () => (await import("./StatusPlaceholder")).StatusPlaceholder,
  /**
   * Repositories, with Connect a repository below it.
   *
   * @returns The page.
   */
  repositories: async () => (await import("./RepositoriesRoutes")).RepositoriesRoutes,
  /**
   * Sign-in: the session lengths.
   *
   * @returns The page.
   */
  "sign-in": async () => (await import("./SignInSettings")).SignInSettings,
};
