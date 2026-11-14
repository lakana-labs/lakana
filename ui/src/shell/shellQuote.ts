/**
 * Quoting for the command lines the interface shows to copy.
 *
 * @packageDocumentation
 */

/**
 * Quotes one argument of a command line the way a POSIX shell reads it back unchanged.
 *
 * @param value - The argument.
 * @returns It in single quotes, each single quote inside as `'\''`.
 */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}
