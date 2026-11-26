/**
 * A violation for `lint-ban.test.ts`: a word chosen by a condition inside JSX. The lint run
 * ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

/**
 * Renders a state, one branch in English only.
 *
 * @param props - The state.
 * @param props.on - Whether it is on.
 * @returns The paragraph.
 */
export function State({ on }: { readonly on: boolean }): JSX.Element {
  const { t } = useTranslation();
  return <p>{on ? t("state.on") : "Off"}</p>;
}
