/**
 * The content security policy the interface is served with.
 *
 * @packageDocumentation
 */

/**
 * The exact `<style>` texts React Aria inserts at run time, as CSP hash sources.
 *
 * @remarks
 * A hash admits only that one text, so `style-src` stays as strict as `'self'`. `usePress`
 * adds `touch-action` for every pressable element; `usePreventScroll` (modals, on iOS and
 * iPadOS) adds `overscroll-behavior: contain`. `build/csp.test.ts` counts the places the
 * libraries insert a `<style>` and hashes their texts, so an upgrade that changes or adds one
 * fails there instead of in a user's console.
 */
export const LIBRARY_STYLE_HASHES: readonly string[] = [
  // react-aria usePress: @layer { [data-react-aria-pressable] { touch-action: pan-x pan-y pinch-zoom; } }
  "'sha256-38RhXrc7EdReTKsOm23ZPOCUgniTUUcjky8QOOrQx6o='",
  // react-aria usePreventScroll: @layer { * { overscroll-behavior: contain; } }
  "'sha256-gYiS/BvZvRcK27JIXTuwhZ3hs2+VJ1X+2gUlE+farlg='",
];

/**
 * The `Content-Security-Policy` header of every page of the interface.
 *
 * @remarks
 * Strict on purpose: only the interface's own origin, no inline script or style (except the
 * library style texts hashed above), no `data:` URL, no frames. A later need is added here with its reason. `vite preview` sends it, so the
 * Playwright tests run under it; the build writes it to `csp.txt` (`build/csp-file.ts`), which
 * lakana-server reads and sends.
 */
export const CONTENT_SECURITY_POLICY: string = [
  "default-src 'self'",
  "script-src 'self'",
  `style-src 'self' ${LIBRARY_STYLE_HASHES.join(" ")}`,
  "img-src 'self'",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");
