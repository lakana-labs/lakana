/**
 * The environments of the Connections screen, each with whether it is protected, so the screen
 * marks a protected one as Queries and Jobs do.
 *
 * @packageDocumentation
 */
import { type ConnectionEnvironmentOut, listConnectionEnvironments } from "../api";
import { listEnvironments } from "../api/environments";

/** An environment whose connections the person may read, and whether it is protected. */
export interface ConnectionEnvironment extends ConnectionEnvironmentOut {
  /** Whether it is protected, which the screen marks in the colour of danger and in words. */
  readonly protected: boolean;
}

/**
 * The names of the protected environments among those the person may read.
 *
 * @returns The names.
 * @throws {ApiError} for a refusal.
 */
export async function readProtected(): Promise<ReadonlySet<string>> {
  const readable = await listEnvironments();
  return new Set(readable.filter((environment) => environment.protected).map(({ name }) => name));
}

/**
 * Reads the environments whose connections the person may read and, beside them, which of the
 * environments are protected: the Connections answer does not say, the environments' list does.
 * Both need `READ_ENVIRONMENT`, so they name the same environments; one that only the first
 * names is not protected. The second read only adds the mark: when it fails, every environment
 * is named plainly, as on a connection's page.
 *
 * @returns The environments, in the Connections answer's order.
 * @throws {ApiError} when the Connections answer is refused.
 */
export async function readConnectionEnvironments(): Promise<readonly ConnectionEnvironment[]> {
  const [listed, protectedNames] = await Promise.all([
    listConnectionEnvironments(),
    readProtected().catch(() => new Set<string>()),
  ]);
  return listed.map((environment) => ({
    ...environment,
    protected: protectedNames.has(environment.name),
  }));
}
