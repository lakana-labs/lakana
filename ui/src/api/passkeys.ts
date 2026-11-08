/**
 * Passkeys through the browser's own WebAuthn (`navigator.credentials`): signing in, adding
 * one, and the passkey touch that confirms a session (step-up).
 *
 * @remarks
 * The server's options arrive as WebAuthn JSON; `PublicKeyCredential.parse*OptionsFromJSON`
 * turns them into what the browser takes, and `credential.toJSON()` turns its answer back.
 * The browser's own refusals pass on as `DOMException`s (`NotAllowedError` when the person
 * cancelled or waited too long); a browser without these methods gets `NotSupportedError`.
 * @packageDocumentation
 */
import { send, setCsrfToken } from "./client";
import type { SignInOut } from "./links";

/** The start of a ceremony, as the options routes answer it. */
interface OptionsOut {
  /** The ceremony's id, sent back with the answer. */
  readonly ceremony_id: string;
  /** The WebAuthn options as JSON. */
  readonly public_key: unknown;
}

/**
 * The browser's `PublicKeyCredential`, or a refusal naming a browser without passkeys.
 *
 * @returns The class.
 * @throws {DOMException} `NotSupportedError` when the browser cannot use passkeys here.
 */
function webAuthn(): typeof PublicKeyCredential {
  const found: unknown = Reflect.get(globalThis, "PublicKeyCredential");
  if (
    (typeof found !== "object" && typeof found !== "function") ||
    found === null ||
    !("parseRequestOptionsFromJSON" in found) ||
    !("parseCreationOptionsFromJSON" in found)
  ) {
    throw new DOMException("This browser cannot use passkeys.", "NotSupportedError");
  }
  return found as typeof PublicKeyCredential;
}

/**
 * The JSON of the browser's answer, or a refusal when it gave none.
 *
 * @param credential - What `navigator.credentials` answered.
 * @returns `credential.toJSON()`.
 * @throws {DOMException} `NotAllowedError` when there is no credential.
 */
function answer(credential: Credential | null): unknown {
  if (credential === null) {
    throw new DOMException("The browser answered without a passkey.", "NotAllowedError");
  }
  return (credential as PublicKeyCredential).toJSON();
}

/**
 * Asks the browser for a passkey touch on the server's request options.
 *
 * @param path - The options route.
 * @returns The ceremony id and the browser's answer.
 */
async function touch(path: string): Promise<{ ceremony_id: string; credential: unknown }> {
  const credentials = webAuthn();
  const options = await send<OptionsOut>("POST", path, {});
  const publicKey = credentials.parseRequestOptionsFromJSON(
    options.public_key as PublicKeyCredentialRequestOptionsJSON,
  );
  const credential = await navigator.credentials.get({ publicKey });
  return { ceremony_id: options.ceremony_id, credential: answer(credential) };
}

/**
 * Signs in with a passkey the browser offers; keeps the new session's CSRF token.
 *
 * @returns Who is signed in.
 */
export async function signInWithPasskey(): Promise<SignInOut> {
  const proof = await touch("/api/v1/sign-in/passkey/options");
  const signedIn = await send<SignInOut>("POST", "/api/v1/sign-in/passkey", proof);
  setCsrfToken(signedIn.csrf_token);
  return signedIn;
}

/** Confirms this browser's session with a touch of one of the person's passkeys. */
export async function stepUp(): Promise<void> {
  const proof = await touch("/api/v1/sign-in/step-up/options");
  await send("POST", "/api/v1/sign-in/step-up", proof);
}

/**
 * Adds a passkey to the person's own account.
 *
 * @param name - The person's own principal name.
 */
export async function addPasskey(name: string): Promise<void> {
  const credentials = webAuthn();
  const base = `/api/v1/principals/${encodeURIComponent(name)}/passkeys`;
  const options = await send<OptionsOut>("POST", `${base}/options`, {});
  const publicKey = credentials.parseCreationOptionsFromJSON(
    options.public_key as PublicKeyCredentialCreationOptionsJSON,
  );
  const credential = await navigator.credentials.create({ publicKey });
  await send("POST", base, { ceremony_id: options.ceremony_id, credential: answer(credential) });
}
