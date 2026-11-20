/**
 * Confirming `lakana login` in the browser: the loopback grant and the device grant.
 *
 * @remarks
 * Its own module, not re-exported from the API's index, so that {@link guarded} stays out of
 * the first load. Nothing here holds a token: the command line exchanges the code itself.
 * @packageDocumentation
 */
import { api } from "./client";
import { guarded } from "./stepUpFirst";

/** The lowest port the command line may listen on. */
const MIN_PORT = 1024;

/** The highest port. */
const MAX_PORT = 65535;

/** What `POST /api/v1/sign-in/cli/approvals` answers. */
export interface CliApprovalOut {
  /** The one-time code for the command line. */
  readonly code: string;
  /** The port to send the browser back to. */
  readonly port: number;
}

/** What `POST /api/v1/sign-in/cli/device/describe` answers. */
export interface CliDeviceHostOut {
  /** The computer the code would sign in, as it named itself. */
  readonly host: string;
  /** The client address that started the sign-in, as the server saw it. */
  readonly requested_from: string;
  /** When it was started. */
  readonly requested_at: string;
}

/**
 * Says whether a number is a port the command line may listen on.
 *
 * @param port - The number.
 * @returns True for a whole number from {@link MIN_PORT} to {@link MAX_PORT}.
 */
export function isLoopbackPort(port: number): boolean {
  return Number.isInteger(port) && port >= MIN_PORT && port <= MAX_PORT;
}

/**
 * Builds the address the browser is sent back to: always `http://127.0.0.1:<port>/`, never
 * anything a link named.
 *
 * @param port - The command line's port.
 * @param params - The query: the code or the error, and the state.
 * @returns The address.
 * @throws {RangeError} for a port outside 1024 to 65535.
 */
export function loopbackUrl(port: number, params: Record<string, string>): string {
  if (!isLoopbackPort(port)) {
    throw new RangeError(
      `Port ${String(port)} is outside ${String(MIN_PORT)} to ${String(MAX_PORT)}.`,
    );
  }
  return `http://127.0.0.1:${String(port)}/?${new URLSearchParams(params).toString()}`;
}

/**
 * Confirms a loopback sign-in, asking for the touch first when the session's is stale.
 *
 * @param port - The command line's port.
 * @param challenge - The S256 challenge of its PKCE verifier.
 * @param host - Its computer's name.
 * @returns The one-time code and the port to send it to.
 * @throws {ApiError} for a refusal; whatever the touch throws, such as a cancelled one.
 */
export async function approveCli(
  port: number,
  challenge: string,
  host: string,
): Promise<CliApprovalOut> {
  return guarded<CliApprovalOut>("POST", "/api/v1/sign-in/cli/approvals", {
    port,
    code_challenge: challenge,
    host,
  });
}

/**
 * Describes the sign-in a device code would approve.
 *
 * @param userCode - The code as the person typed it.
 * @returns The computer's name, the address that asked and when.
 * @throws {ApiError} 404 when no sign-in waits for the code; 429 past too many wrong codes.
 */
export async function describeDevice(userCode: string): Promise<CliDeviceHostOut> {
  return api.request<CliDeviceHostOut>("POST", "/api/v1/sign-in/cli/device/describe", {
    user_code: userCode,
  });
}

/**
 * Approves a device sign-in, asking for the touch first when the session's is stale.
 *
 * @param userCode - The code.
 * @throws {ApiError} for a refusal; whatever the touch throws.
 */
export async function approveDevice(userCode: string): Promise<void> {
  await guarded<undefined>("POST", "/api/v1/sign-in/cli/device/approve", { user_code: userCode });
}

/**
 * Refuses a device sign-in.
 *
 * @param userCode - The code.
 * @throws {ApiError} for a refusal.
 */
export async function denyDevice(userCode: string): Promise<void> {
  await api.request<undefined>("POST", "/api/v1/sign-in/cli/device/deny", { user_code: userCode });
}
