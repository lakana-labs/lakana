/**
 * A device code the person typed on `/sign-in/device`, kept in this tab's `sessionStorage` only
 * while the browser confirms at the identity provider: the page takes it when it opens again,
 * and it is forgotten when the approval is answered, on Cancel, when the confirmation is refused,
 * and when the page is left or shown again from the browser's history.
 *
 * @packageDocumentation
 */

/** The `sessionStorage` key of a typed device code while the browser confirms at the provider. */
const KEPT_CODE_KEY = "lakana.cliSignIn.deviceCode";

/**
 * Keeps a typed device code in this tab for the page's return from the identity provider, or
 * forgets it.
 *
 * @param code - The code, or null to forget it.
 */
export function keepCode(code: string | null): void {
  try {
    if (code === null) {
      sessionStorage.removeItem(KEPT_CODE_KEY);
    } else {
      sessionStorage.setItem(KEPT_CODE_KEY, code);
    }
  } catch {
    // Storage is blocked: the person types the code again.
  }
}

/**
 * Takes the device code this tab kept, once.
 *
 * @returns The code, or empty.
 */
export function takeKeptCode(): string {
  try {
    const kept = sessionStorage.getItem(KEPT_CODE_KEY) ?? "";
    sessionStorage.removeItem(KEPT_CODE_KEY);
    return kept;
  } catch {
    return "";
  }
}

/** Forgets a kept device code: the confirmation it waited for was refused. */
export function forgetKeptCode(): void {
  keepCode(null);
}
