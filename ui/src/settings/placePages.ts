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
   * Status: health, workers and version; compute, memory, workers and scheduler in detail.
   *
   * @returns The page.
   */
  status: async () => (await import("./StatusPage")).StatusPage,
  /**
   * Environments, with an environment's page below it.
   *
   * @returns The page.
   */
  environments: async () => (await import("./EnvironmentsRoutes")).EnvironmentsRoutes,
  /**
   * Repositories, with Connect a repository below it.
   *
   * @returns The page.
   */
  repositories: async () => (await import("./RepositoriesRoutes")).RepositoriesRoutes,
  /**
   * CI trust: the rules that let CI jobs deploy with their ID token, each rule's page, and
   * Explain a CI job's claims.
   *
   * @returns The page.
   */
  "ci-trust": async () => (await import("./CiTrustRoutes")).CiTrustRoutes,
  /**
   * People, with a person's page below it.
   *
   * @returns The page.
   */
  people: async () => (await import("../access/PeopleRoutes")).PeopleRoutes,
  /**
   * Groups, with a group's page below it.
   *
   * @returns The page.
   */
  groups: async () => (await import("../access/GroupsRoutes")).GroupsRoutes,
  /**
   * Grants: every grant, filtered, with Remove and Add grant.
   *
   * @returns The page.
   */
  grants: async () => (await import("../access/GrantsPage")).GrantsPage,
  /**
   * Tokens: everyone's live personal tokens and the task tokens, each revocable.
   *
   * @returns The page.
   */
  tokens: async () => (await import("../access/TokensPage")).TokensPage,
  /**
   * Secrets: an environment's secrets, their names and uses, never values.
   *
   * @returns The page.
   */
  secrets: async () => (await import("../access/SecretsPage")).SecretsPage,
  /**
   * Row filters & masks: every row filter and column mask, filtered, with Add and Delete.
   *
   * @returns The page.
   */
  "filters-and-masks": async () =>
    (await import("../access/FiltersAndMasksPage")).FiltersAndMasksPage,
  /**
   * Access file: the managed scopes, the changes by hand, and Download access file.
   *
   * @returns The page.
   */
  "access-file": async () => (await import("../access/AccessFilePage")).AccessFilePage,
  /**
   * What I may do: one's own grants and what they add up to.
   *
   * @returns The page.
   */
  "what-i-may-do": async () => (await import("../access/WhatIMayDo")).WhatIMayDo,
  /**
   * Sign-in: the session lengths.
   *
   * @returns The page.
   */
  "sign-in": async () => (await import("./SignInSettings")).SignInSettings,
  /**
   * Invites: the open invites and Invite someone.
   *
   * @returns The page.
   */
  invites: async () => (await import("../access/InvitesPage")).InvitesPage,
  /**
   * Recovery links: the open links and Issue a recovery link.
   *
   * @returns The page.
   */
  "recovery-links": async () => (await import("../access/RecoveryLinksPage")).RecoveryLinksPage,
  /**
   * Configuration: the running server's settings, read-only.
   *
   * @returns The page.
   */
  configuration: async () => (await import("./ConfigurationPage")).ConfigurationPage,
};
