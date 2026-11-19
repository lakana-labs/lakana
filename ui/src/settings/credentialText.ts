/**
 * How a repository authenticates to its host, in a few words.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { RepositoryOut } from "../api";
import { markedText } from "../shell/VisibleText";

/**
 * Whether a repository that needs a credential has none the person can see: no secret and no
 * deploy key in an environment they read.
 *
 * @param repo - The repository.
 * @returns The answer; never for a local folder.
 */
export function lacksCredential(repo: RepositoryOut): boolean {
  return repo.kind !== "local" && repo.credential_secret === null && repo.deploy_keys.length === 0;
}

/**
 * The repository's credential: its deploy keys, the secret that holds its SSH key or token, or
 * none.
 *
 * @param t - The translation function.
 * @param repo - The repository.
 * @returns The words.
 */
export function credentialText(t: TFunction, repo: RepositoryOut): string {
  const p = "settings.repositoryPages.credential";
  if (repo.kind === "local") return t(`${p}.local`);
  if (repo.kind === "ssh" && repo.deploy_keys.length > 0) {
    return t(`${p}.deployKeys`, { count: repo.deploy_keys.length });
  }
  if (repo.credential_secret === null) return t(`${p}.none`);
  return t(repo.kind === "ssh" ? `${p}.sshSecret` : `${p}.token`, {
    secret: markedText(repo.credential_secret),
  });
}
