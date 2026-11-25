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
 * Decodes percent escapes once, as Python's `urllib.parse.unquote` does: each run of `%XX`
 * escapes is read as UTF-8 (a byte that is no UTF-8 becomes U+FFFD), and a `%` that starts no
 * escape stays as it is, while the escapes around it are still decoded.
 *
 * @param text - The text.
 * @returns The decoded text.
 */
function decodedOnce(text: string): string {
  return text.replace(/(?:%[0-9A-Fa-f]{2})+/g, (run) => {
    const bytes = new Uint8Array(run.length / 3);
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Number.parseInt(run.slice(index * 3 + 1, index * 3 + 3), 16);
    }
    // As `unquote`, a byte-order mark is kept as a character, never dropped.
    return new TextDecoder("utf-8", { ignoreBOM: true }).decode(bytes);
  });
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
  const query = path.indexOf("?");
  const confirmation = CLI_CONFIRMATIONS.has(query === -1 ? path : path.slice(0, query));
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
