/**
 * Whether a path the page kept stays inside the interface: the server's rule for a page to come
 * back to (`lakana_core.identity.oidc_flows.safe_return_to`), in the browser.
 *
 * @packageDocumentation
 */

/** The longest path kept, as the server keeps it. */
const MAX_PATH = 2048;

/** The sign-in pages a sign-in may come back to: `lakana login`'s two confirmations. */
const CLI_CONFIRMATIONS = new Set(["/sign-in/cli", "/sign-in/device"]);

/**
 * A control or format character, which the browser's URL parser drops or a path must not hold
 * (`/\t/evil.example` reads as `//evil.example`).
 */
const HIDDEN = /[\p{Cc}\p{Cf}]/u;

/**
 * Decodes percent escapes once, keeping a malformed one as it is.
 *
 * @param text - The text.
 * @returns The decoded text.
 */
function decodedOnce(text: string): string {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

/**
 * Whether a path stays inside the interface: it starts with exactly one `/`, holds no `//`, no
 * `\`, no control or format character, also percent-encoded (three times decoded), is at most
 * 2048 characters, and is no sign-in page but `lakana login`'s two confirmations (spelt exactly,
 * with no fragment), as the server's `safe_return_to` decides.
 *
 * @param path - The path.
 * @returns True for a local path.
 */
export function isLocalPath(path: unknown): path is string {
  if (typeof path !== "string" || path.length > MAX_PATH || !path.startsWith("/")) {
    return false;
  }
  const confirmation = CLI_CONFIRMATIONS.has(path.split("?")[0] ?? "");
  let spelling = path;
  for (let round = 0; round < 4; round += 1) {
    if (
      spelling.includes("//") ||
      spelling.includes("\\") ||
      HIDDEN.test(spelling) ||
      (confirmation && spelling.includes("#")) ||
      (!confirmation && spelling.toLowerCase().startsWith("/sign-in"))
    ) {
      return false;
    }
    spelling = decodedOnce(spelling);
  }
  return true;
}
