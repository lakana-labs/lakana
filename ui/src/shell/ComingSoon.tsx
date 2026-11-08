/**
 * The state of an area whose screen comes in a later part: the command line to use until then.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { CopyLine } from "./CopyLine";

/** Properties of {@link ComingSoon}. */
export interface ComingSoonProps {
  /** The part of the interface that brings the screen. */
  readonly part: number;
  /** The command line that does the job meanwhile. */
  readonly cli: string;
}

/**
 * Names the part, shows the command line and offers one action: copy it.
 *
 * @param props - The part and the command line.
 * @param props.part - The part that brings the screen.
 * @param props.cli - The command line.
 * @returns The state.
 */
export function ComingSoon({ part, cli }: ComingSoonProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <section className="coming-soon">
      <p>{t("comingSoon.part", { part })}</p>
      <p>{t("comingSoon.useCli")}</p>
      <CopyLine text={cli} copyLabel={t("comingSoon.copy")} />
    </section>
  );
}
