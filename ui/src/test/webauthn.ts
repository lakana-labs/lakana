/**
 * A fake WebAuthn for tests: jsdom has no `PublicKeyCredential` and no `navigator.credentials`.
 *
 * @packageDocumentation
 */
import { vi } from "vitest";

/** The fake browser credential API a test drives. */
export interface FakeWebAuthn {
  /** `navigator.credentials.create`. */
  readonly create: ReturnType<typeof vi.fn>;
  /** `navigator.credentials.get`. */
  readonly get: ReturnType<typeof vi.fn>;
  /** `PublicKeyCredential.parseCreationOptionsFromJSON`. */
  readonly parseCreation: ReturnType<typeof vi.fn>;
  /** `PublicKeyCredential.parseRequestOptionsFromJSON`. */
  readonly parseRequest: ReturnType<typeof vi.fn>;
}

/** What the fake authenticator's credential answers to `toJSON()`. */
export const CREDENTIAL_JSON = { id: "cred-1", type: "public-key", response: {} };

/**
 * A credential whose `toJSON()` answers {@link CREDENTIAL_JSON}.
 *
 * @returns The credential.
 */
function credential(): { toJSON: () => unknown } {
  return { toJSON: () => CREDENTIAL_JSON };
}

/**
 * Installs a fake WebAuthn whose authenticator answers every ceremony.
 *
 * @returns The fakes, to assert calls or make one fail.
 */
export function fakeWebAuthn(): FakeWebAuthn {
  const fake: FakeWebAuthn = {
    create: vi.fn(() => Promise.resolve(credential())),
    get: vi.fn(() => Promise.resolve(credential())),
    parseCreation: vi.fn((options: unknown) => ({ parsed: "creation", options })),
    parseRequest: vi.fn((options: unknown) => ({ parsed: "request", options })),
  };
  vi.stubGlobal("PublicKeyCredential", {
    parseCreationOptionsFromJSON: fake.parseCreation,
    parseRequestOptionsFromJSON: fake.parseRequest,
  });
  Object.defineProperty(navigator, "credentials", {
    configurable: true,
    value: { create: fake.create, get: fake.get },
  });
  return fake;
}
