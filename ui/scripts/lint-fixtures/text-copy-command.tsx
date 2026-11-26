/**
 * No violation, for `lint-ban.test.ts`: a command to copy is the same in every language, so
 * `CopyLine`'s `text` is not read as words. The lint run ignores this directory; the test lints it
 * on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

/**
 * A line to copy, as `ui/src/shell/CopyLine.tsx` takes one.
 *
 * @param props - The line.
 * @param props.text - The command.
 * @param props.copyLabel - The copy button's name.
 * @returns The line.
 */
function CopyLine({
  text,
  copyLabel,
}: {
  readonly text: string;
  readonly copyLabel: string;
}): JSX.Element {
  return (
    <button type="button" aria-label={copyLabel}>
      {text}
    </button>
  );
}

/**
 * Renders the command that lists the connections.
 *
 * @returns The line.
 */
export function ListCommand(): JSX.Element {
  const { t } = useTranslation();
  return <CopyLine text="lakana connection list --env <environment>" copyLabel={t("copy")} />;
}
