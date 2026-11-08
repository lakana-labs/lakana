/**
 * Writes the content security policy into the build, where lakana-server reads it.
 *
 * @packageDocumentation
 */
import { CONTENT_SECURITY_POLICY } from "../csp.ts";

/** The file at the top of the build that holds the policy, as the header's exact text. */
export const CSP_FILE = "csp.txt";

/** What the plugin needs of Rollup's plugin context: emitting one file. */
interface EmitContext {
  /** Adds a file to the build. */
  emitFile(file: { type: "asset"; fileName: string; source: string }): unknown;
}

/** The Vite plugin `cspFile` returns. */
export interface CspFilePlugin {
  /** The plugin's name in Vite's messages. */
  readonly name: string;
  /** Emits `csp.txt` with the policy once the bundle is written. */
  generateBundle(this: EmitContext): void;
}

/**
 * A Vite plugin that writes `CONTENT_SECURITY_POLICY` to `csp.txt` in the build.
 *
 * @remarks
 * One source for the policy: `vite preview` sends the constant, and lakana-server sends the
 * file of the build it serves, so the header always matches the interface it protects.
 * @returns The plugin.
 */
export function cspFile(): CspFilePlugin {
  return {
    name: "lakana-csp-file",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: CSP_FILE, source: CONTENT_SECURITY_POLICY });
    },
  };
}
