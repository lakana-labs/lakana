/**
 * What a secret is used for, in words.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { SecretOut } from "../api/secrets";
import "./secretStrings";

/** Where the Secrets page remembers the environment last chosen. */
export const SECRETS_ENVIRONMENT_KEY = "lakana.secrets.environment";

/** The environments the Secrets page offers, and the one it shows. */
export interface SecretsEnvironment {
  /** The environments where the person manages secrets. */
  readonly allowed: readonly string[];
  /** The one shown: the linked one if allowed, else the remembered one if allowed, else the first. */
  readonly chosen: string;
  /** Whether the address names an environment the person cannot manage. */
  readonly refused: boolean;
}

/**
 * Chooses the environment the Secrets page shows.
 *
 * @param manages - Where the person manages secrets; null while not known.
 * @param linked - The address's environment, or null.
 * @param remembered - The environment remembered, or null.
 * @returns The environments offered, the one shown and whether the link was refused.
 */
export function chooseEnvironment(
  manages: readonly string[] | null,
  linked: string | null,
  remembered: string | null,
): SecretsEnvironment {
  const allowed = manages ?? [];
  const chosen =
    [linked, remembered].find(
      (candidate): candidate is string => candidate !== null && allowed.includes(candidate),
    ) ??
    allowed[0] ??
    "";
  return { allowed, chosen, refused: linked !== null && !allowed.includes(linked) };
}

/**
 * Says what a secret is used for: its package index, repository or connection by name, its
 * environment's storage and webhooks, or no use yet.
 *
 * @param t - The translator.
 * @param secret - The secret.
 * @returns The words.
 */
export function secretUseText(
  t: TFunction,
  secret: Pick<SecretOut, "use" | "use_target" | "environment">,
): string {
  if (secret.use === null) return t("secrets.uses.none");
  if (secret.use === "environment") {
    return t("secrets.uses.environment", { environment: secret.environment });
  }
  return secret.use_target === null
    ? t(`secrets.uses.${secret.use}Gone`)
    : t(`secrets.uses.${secret.use}`, { target: secret.use_target });
}
