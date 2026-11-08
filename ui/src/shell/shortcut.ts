/**
 * Which modifier the Search button names: ⌘ on Apple platforms, Ctrl elsewhere.
 *
 * @packageDocumentation
 */

/** What {@link shortcutLabel} reads of `navigator`. */
export type PlatformHints = Pick<Navigator, "platform" | "userAgent"> & {
  /** The client hints platform, where the browser has it (Chromium). */
  readonly userAgentData?: { readonly platform: string };
};

const APPLE = /mac|iphone|ipad|ipod|ios/i;

/**
 * Names the platform family for the shortcut label. Both Ctrl+K and ⌘ K work everywhere;
 * this only picks the one the button shows.
 *
 * @param nav - The browser's `navigator`.
 * @returns `"mac"` on macOS, iPadOS and iOS, otherwise `"other"`.
 */
export function shortcutLabel(nav: PlatformHints): "mac" | "other" {
  // `||`, not `??`: Chromium reports an empty client hints platform on some systems.
  const platform = nav.userAgentData?.platform || nav.platform || nav.userAgent;
  return APPLE.test(platform) ? "mac" : "other";
}
