/**
 * The instance's sign-in settings: how long a browser session lasts, and with an identity
 * provider its name, counts and the token window.
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
  /** The shortest token window, in days; absent from a server older than #32. */
  readonly token_provider_window_days_min?: number;
  /** The longest token window, in days; absent from a server older than #32. */
  readonly token_provider_window_days_max?: number;
}

/** The identity provider people sign in through, with its counts (administrators only). */
export interface SignInProviderOut {
  /** The name on the sign-in button. */
  readonly display_name: string;
  /** The provider's issuer (`LAKANA_OIDC__ISSUER`). */
  readonly issuer: string;
  /** How many active people are linked to it. */
  readonly linked_people: number;
  /** How many administrators can sign in without it: the way in when it is down. */
  readonly unlinked_admins: number;
}

/** The session lengths, as `GET /api/v1/settings/sign-in` answers them. */
export interface SignInSettingsOut {
  /** How long a session lasts without a request the person started, in minutes. */
  readonly idle_timeout_minutes: number;
  /** How long a session lasts at most, in minutes. */
  readonly absolute_lifetime_minutes: number;
  /** The limits both may be chosen between. */
  readonly bounds: SignInBoundsOut;
  /**
   * How long a linked person's personal API tokens work after their last sign-in through the
   * identity provider, in days; absent from a server older than #32.
   */
  readonly token_provider_window_days?: number;
  /** The identity provider and its counts while it is on; null (or absent) otherwise. */
  readonly provider?: SignInProviderOut | null;
}

/** New session lengths, in minutes. */
export interface SignInSettingsIn {
  /** The new idle timeout. */
  readonly idle_timeout_minutes: number;
  /** The new absolute lifetime. */
  readonly absolute_lifetime_minutes: number;
  /** The new token window of people linked to the identity provider, in days (1 to 365). */
  readonly token_provider_window_days?: number;
}

/**
 * Reads the session lengths and their limits.
 *
 * @returns The settings.
 */
export async function getSignInSettings(): Promise<SignInSettingsOut> {
  return api.request<SignInSettingsOut>("GET", "/api/v1/settings/sign-in");
}
