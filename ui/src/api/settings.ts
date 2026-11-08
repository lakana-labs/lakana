/**
 * The instance's sign-in settings: how long a browser session lasts.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** The limits the session lengths may be chosen between, in minutes. */
interface SignInBoundsOut {
  /** The shortest idle timeout. */
  readonly idle_timeout_minutes_min: number;
  /** The longest idle timeout. */
  readonly idle_timeout_minutes_max: number;
  /** The shortest absolute lifetime. */
  readonly absolute_lifetime_minutes_min: number;
  /** The longest absolute lifetime. */
  readonly absolute_lifetime_minutes_max: number;
}

/** The session lengths, as `GET /api/v1/settings/sign-in` answers them. */
export interface SignInSettingsOut {
  /** How long a session lasts without a request the person started, in minutes. */
  readonly idle_timeout_minutes: number;
  /** How long a session lasts at most, in minutes. */
  readonly absolute_lifetime_minutes: number;
  /** The limits both may be chosen between. */
  readonly bounds: SignInBoundsOut;
}

/** New session lengths, in minutes. */
export interface SignInSettingsIn {
  /** The new idle timeout. */
  readonly idle_timeout_minutes: number;
  /** The new absolute lifetime. */
  readonly absolute_lifetime_minutes: number;
}

/**
 * Reads the session lengths and their limits.
 *
 * @returns The settings.
 */
export async function getSignInSettings(): Promise<SignInSettingsOut> {
  return api.request<SignInSettingsOut>("GET", "/api/v1/settings/sign-in");
}

/**
 * Changes the session lengths; sessions that begin afterwards use them.
 *
 * @param lengths - The new lengths.
 * @returns The settings as saved.
 */
export async function changeSignInSettings(lengths: SignInSettingsIn): Promise<SignInSettingsOut> {
  return api.request<SignInSettingsOut>("PATCH", "/api/v1/settings/sign-in", lengths);
}
