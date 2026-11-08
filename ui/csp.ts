/**
 * The content security policy the interface is served with.
 *
 * @packageDocumentation
 */

/**
 * The `Content-Security-Policy` header of every page of the interface.
 *
 * @remarks
 * Strict on purpose: only the interface's own origin, no inline script or style, no `data:`
 * URL, no frames. A later need is added here with its reason. `vite preview` sends it, so the
 * Playwright tests run under it, and lakana-server sends the same value.
 */
export const CONTENT_SECURITY_POLICY: string = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self'",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");
