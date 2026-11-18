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
