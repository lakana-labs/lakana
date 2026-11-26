/**
 * CI trust rules, which let a CI job deploy as a service account with its OIDC ID token, and
 * the run-as account of a deployment.
 *
 * @remarks
 * Only an instance administrator reads or changes the rules. Every change of a rule needs a
 * fresh passkey touch, asked for before the request (`guarded`): a rule lets CI act as its
 * account. Binding a deployment's run-as account needs one too; clearing it removes rights and
 * needs none. Explain reads only: it compares the claims given and records nothing. No request
 * here carries `override_managed`: the interface never overrides a managed scope.
 * @packageDocumentation
 */
import { api } from "./client";
import type { DeploymentOut } from "./jobs";
import { environmentPath } from "./manage";
import { guarded } from "./stepUpFirst";

/** What a rule may allow. */
export type CiTrustKind = "deploy" | "dry_run";

/** A CI trust rule, as `GET /api/v1/ci-trust-rules` answers it. */
export interface CiTrustRuleOut {
  /** The rule's id. */
  readonly id: string;
  /** The issuer's URL. */
  readonly issuer: string;
  /** The service account the CI job acts as. */
  readonly principal: string;
  /** The environment the job may deploy to. */
  readonly environment: string;
  /** The repository it may deploy. */
  readonly repository: string;
  /** What it allows: `deploy` and/or `dry_run`. */
  readonly kinds: readonly CiTrustKind[];
  /** Claim name to the exact text the ID token's claim must have. */
  readonly conditions: Readonly<Record<string, string>>;
  /** Whether it accepts ID tokens. */
  readonly enabled: boolean;
  /** Who created it; null once that account is gone. */
  readonly created_by: string | null;
  /** When it was created. */
  readonly created_at: string;
  /** Who changed it last; null when nobody has, or that account is gone. */
  readonly updated_by: string | null;
  /** When it last changed. */
  readonly updated_at: string;
  /** Whether a deploy under it starts the project's `on_deploy` jobs. */
  readonly start_on_deploy_jobs: boolean;
}

/** What creating a rule sends. */
export interface CiTrustRuleCreate {
  /** `github`, `gitlab` or a configured issuer's URL. */
  readonly issuer: string;
  /** The service account. */
  readonly principal: string;
  /** The environment. */
  readonly environment: string;
  /** The repository. */
  readonly repository: string;
  /** What it allows. */
  readonly kinds: readonly CiTrustKind[];
  /** The claim conditions. */
  readonly conditions: Readonly<Record<string, string>>;
  /** Whether its deploys start the project's `on_deploy` jobs. */
  readonly start_on_deploy_jobs: boolean;
}

/** What changing a rule sends: only the fields that change. */
export type CiTrustRuleChanges = Partial<
  Pick<CiTrustRuleCreate, "principal" | "kinds" | "conditions" | "start_on_deploy_jobs">
>;

/** What Explain sends. */
export interface CiTrustExplainIn {
  /** The ID token's claims, decoded. */
  readonly claims: Readonly<Record<string, unknown>>;
  /** The environment the exchange would ask for. */
  readonly environment: string;
  /** The repository it would ask for. */
  readonly repository: string;
  /** The issuer; the claims' `iss` when left out. */
  readonly issuer?: string;
}

/** Why one rule does or does not accept the claims. */
interface CiTrustRuleExplanationOut {
  /** The rule. */
  readonly rule_id: string;
  /** Whether it accepts them. */
  readonly matches: boolean;
  /** Why not, one sentence each; empty when it matches. */
  readonly reasons: readonly string[];
}

/** What an exchange with the claims would do. */
export interface CiTrustExplainOut {
  /** The issuer compared. */
  readonly issuer: string;
  /** The environment asked for. */
  readonly environment: string;
  /** The repository asked for. */
  readonly repository: string;
  /** One sentence: the rule that accepts the claims, or why the exchange is refused. */
  readonly verdict: string;
  /** The ids of the rules that match. */
  readonly matching: readonly string[];
  /** Every rule, with its reasons. */
  readonly rules: readonly CiTrustRuleExplanationOut[];
}

/** The path of the rules. */
const RULES = "/api/v1/ci-trust-rules";

/**
 * The API path of a rule or one of its parts.
 *
 * @param id - The rule's id.
 * @param suffix - What follows it, such as `/enable`.
 * @returns The path.
 */
function rulePath(id: string, suffix = ""): string {
  return `${RULES}/${encodeURIComponent(id)}${suffix}`;
}

/**
 * Lists every rule, oldest first.
 *
 * @returns The rules.
 * @throws {ApiError} for a refusal.
 */
export function listCiTrustRules(): Promise<readonly CiTrustRuleOut[]> {
  return api.request<readonly CiTrustRuleOut[]>("GET", RULES);
}

/**
 * Reads one rule.
 *
 * @param id - The rule's id.
 * @returns The rule.
 * @throws {ApiError} for a refusal, such as 404 for no such rule.
 */
export function getCiTrustRule(id: string): Promise<CiTrustRuleOut> {
  return api.request<CiTrustRuleOut>("GET", rulePath(id));
}

/**
 * Creates an enabled rule, after the passkey touch.
 *
 * @param body - The rule.
 * @returns The rule.
 * @throws {ApiError} for a refusal, such as 422 for conditions the server does not accept.
 */
export function createCiTrustRule(body: CiTrustRuleCreate): Promise<CiTrustRuleOut> {
  return guarded<CiTrustRuleOut>("POST", RULES, body);
}

/**
 * Changes a rule, after the passkey touch; the tokens issued under it end.
 *
 * @param id - The rule's id.
 * @param changes - The fields that change.
 * @returns The rule.
 * @throws {ApiError} for a refusal.
 */
export function updateCiTrustRule(
  id: string,
  changes: CiTrustRuleChanges,
): Promise<CiTrustRuleOut> {
  return guarded<CiTrustRuleOut>("PATCH", rulePath(id), changes);
}

/**
 * Enables or disables a rule, after the passkey touch.
 *
 * @param id - The rule's id.
 * @param enabled - Whether it accepts ID tokens from now on.
 * @returns The rule.
 * @throws {ApiError} for a refusal, such as 422 for a rule whose conditions no longer pass.
 */
export function setCiTrustRuleEnabled(id: string, enabled: boolean): Promise<CiTrustRuleOut> {
  return guarded<CiTrustRuleOut>("POST", rulePath(id, enabled ? "/enable" : "/disable"));
}

/**
 * Deletes a rule, after the passkey touch; the tokens issued under it end.
 *
 * @param id - The rule's id.
 * @throws {ApiError} for a refusal.
 */
export async function deleteCiTrustRule(id: string): Promise<void> {
  await guarded("DELETE", rulePath(id));
}

/**
 * Says which rule an exchange with the claims would match, and why the others do not. It
 * changes and records nothing, so it needs no passkey touch.
 *
 * @param body - The claims, the environment, the repository and the issuer if given.
 * @returns The verdict and every rule's reasons.
 * @throws {ApiError} for a refusal, such as 422 without an issuer or an `iss` claim.
 */
export function explainCiTrust(body: CiTrustExplainIn): Promise<CiTrustExplainOut> {
  return api.request<CiTrustExplainOut>("POST", `${RULES}/explain`, body);
}

/**
 * The API path of a deployment's run-as account.
 *
 * @param environment - The environment.
 * @param repository - The repository.
 * @returns The path.
 */
function runAsPath(environment: string, repository: string): string {
  return environmentPath(environment, `/deployments/${encodeURIComponent(repository)}/run-as`);
}

/**
 * Binds the service account a deployment's runs write as, after the passkey touch.
 *
 * @param environment - The environment.
 * @param repository - The deployed repository.
 * @param principal - An active service account's name.
 * @returns The deployment with its binding.
 * @throws {ApiError} for a refusal, such as 404 for a name that is no service account.
 */
export function setRunAs(
  environment: string,
  repository: string,
  principal: string,
): Promise<DeploymentOut> {
  return guarded<DeploymentOut>("PUT", runAsPath(environment, repository), { principal });
}

/**
 * Removes a deployment's run-as account. It removes rights, so it needs no passkey touch.
 *
 * @param environment - The environment.
 * @param repository - The deployed repository.
 * @returns The deployment without a binding.
 * @throws {ApiError} for a refusal.
 */
export function clearRunAs(environment: string, repository: string): Promise<DeploymentOut> {
  return api.request<DeploymentOut>("DELETE", runAsPath(environment, repository));
}
