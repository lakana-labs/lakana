/**
 * The patterns of the bundler's chunk groups (`vite.config.ts`, `codeSplitting.groups`).
 *
 * @packageDocumentation
 */

/**
 * The files the `shell` chunk keeps together: the shell's Page and not-found state, and React
 * Aria's Button, which Settings' places would otherwise split off.
 *
 * @remarks
 * It names a file inside react-aria-components (`dist/private/Button.mjs`). If an update moves
 * that file, the group stops matching and the first load grows without the size gate saying why;
 * `chunk-groups.test.ts` fails first.
 */
export const SHELL_CHUNK =
  /src[\\/]shell[\\/](Page|NotFound)\.tsx|react-aria-components[\\/]dist[\\/]private[\\/]Button\.mjs/;
