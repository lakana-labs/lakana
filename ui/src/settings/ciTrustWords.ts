/**
 * What Settings › CI trust's pages share: their addresses, the issuers and kinds in words, and
 * the claims a new rule starts with.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { CiTrustKind } from "../api/ciTrust";

/** Settings › CI trust. */
export const CI_TRUST_PATH = "/settings/ci-trust";

/** Explain a CI job's claims. */
export const EXPLAIN_PATH = `${CI_TRUST_PATH}/explain`;

/** GitHub Actions' issuer, as the server stores a rule's. */
export const GITHUB_ISSUER = "https://token.actions.githubusercontent.com";

/** GitLab.com's issuer. */
export const GITLAB_ISSUER = "https://gitlab.com";

/** An issuer as the form chooses it: one of the two presets, or another by its URL. */
export type IssuerPreset = "github" | "gitlab" | "other";

/** One claim condition as the form holds it. */
export interface ConditionRow {
  /** The claim's name. */
  readonly name: string;
  /** The text it must equal. */
  readonly value: string;
}

/** What an address asks Add rule to start from. */
export interface AddRuleStart {
  /** The repository, or null. */
  readonly repository: string | null;
  /** The environment, or null. */
  readonly environment: string | null;
  /** The issuer preset, or null. */
  readonly issuer: "github" | "gitlab" | null;
}

/**
 * The address of a rule's page.
 *
 * @param id - The rule's id.
 * @returns The path.
 */
export function ciTrustRulePath(id: string): string {
  return `${CI_TRUST_PATH}/${encodeURIComponent(id)}`;
}

/** What the end of Connect a repository fills Add rule in with. */
export interface AddRuleLink {
  /** The repository just connected. */
  readonly repository: string;
  /** The environment whose access was tested. */
  readonly environment: string;
  /** The host's CI. */
  readonly issuer: "github" | "gitlab";
}

/**
 * The address that opens Add rule filled in, as the end of Connect a repository links to it.
 *
 * @param start - The repository, the environment and the issuer.
 * @returns The path with its query.
 */
export function addRulePath(start: AddRuleLink): string {
  const query = new URLSearchParams({ add: "1", ...start });
  return `${CI_TRUST_PATH}?${query.toString()}`;
}

/**
 * Reads what Add rule starts from, when the address asks for it.
 *
 * @param params - The address's query.
 * @returns The start, or null when the address does not open Add rule.
 */
export function readAddRule(params: URLSearchParams): AddRuleStart | null {
  if (params.get("add") !== "1") return null;
  const issuer = params.get("issuer");
  return {
    repository: params.get("repository"),
    environment: params.get("environment"),
    issuer: issuer === "github" || issuer === "gitlab" ? issuer : null,
  };
}

/**
 * The preset an issuer's URL is.
 *
 * @param issuer - The issuer's URL.
 * @returns The preset, or `other`.
 */
export function issuerPreset(issuer: string): IssuerPreset {
  if (issuer === GITHUB_ISSUER) return "github";
  return issuer === GITLAB_ISSUER ? "gitlab" : "other";
}

/**
 * An issuer in words: GitHub Actions, GitLab.com, or another by its URL.
 *
 * @param t - The translator.
 * @param issuer - The issuer's URL.
 * @returns The words.
 */
export function issuerWords(t: TFunction, issuer: string): string {
  const preset = issuerPreset(issuer);
  return preset === "other" ? issuer : t(`settings.ciTrust.issuers.${preset}`);
}

/**
 * What a rule allows, in words: deploy first.
 *
 * @param t - The translator.
 * @param kinds - The kinds.
 * @returns The words; empty for none.
 */
export function kindsWords(t: TFunction, kinds: readonly CiTrustKind[]): string {
  const words = (["deploy", "dry_run"] as const)
    .filter((kind) => kinds.includes(kind))
    .map((kind) => t(`settings.ciTrust.kinds.${kind}`));
  if (words.length === 2) {
    return t("settings.ciTrust.kindsAnd", { first: words[0], second: words[1] });
  }
  return words[0] ?? "";
}

/**
 * The claims a new rule starts with, which the server requires of each: the repository's or
 * project's number (left to fill in), the main branch, and how the run was started.
 *
 * @param issuer - The issuer chosen.
 * @returns The rows.
 */
export function startingConditions(issuer: IssuerPreset): readonly ConditionRow[] {
  return issuer === "github"
    ? [
        { name: "repository_id", value: "" },
        { name: "ref", value: "refs/heads/main" },
        { name: "event_name", value: "push" },
      ]
    : [
        { name: "project_id", value: "" },
        { name: "ref", value: "main" },
        { name: "ref_protected", value: "true" },
      ];
}

/**
 * The key of the sentence that says which claims the issuer's rules must pin.
 *
 * @param issuer - The issuer chosen.
 * @returns The key.
 */
export function conditionsHelpKey(issuer: IssuerPreset): string {
  return issuer === "github"
    ? "settings.ciTrust.dialog.conditionsHelpGithub"
    : "settings.ciTrust.dialog.conditionsHelpGitlab";
}
