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

/**
 * The files the `form` chunk keeps together: React Aria's form code (the field and its
 * validation, the form, the group and the text field).
 *
 * @remarks
 * The environment picker's Select uses the validation but not the text field, so once a screen
 * loaded on demand imports the picker, the bundler would split this code into two chunks, both in
 * the first load, and the exports between them cost about 0.39 kB. It names files inside
 * react-aria-components, react-aria and react-stately; `chunk-groups.test.ts` fails first if an
 * update moves one.
 */
export const FORM_CHUNK =
  /react-aria-components[\\/]dist[\\/]private[\\/](FieldError|Form|Group|Input)\.mjs|react-stately[\\/]dist[\\/]private[\\/]form[\\/]useFormValidationState\.mjs|react-aria[\\/]dist[\\/]private[\\/](label[\\/]useField|utils[\\/]useFormReset|form[\\/]useFormValidation|textfield[\\/]useTextField)\.mjs/;
