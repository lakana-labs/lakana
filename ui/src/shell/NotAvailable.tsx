/**
 * The state of an area, or a place in one, the user may not use: a deep link to it lands here.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Link } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { Area } from "./areas";
import { Page } from "./Page";

/** Properties of {@link NotAvailable}: the area, or the place inside one, the user may not use. */
export type NotAvailableProps =
  | {
      /** The area the user may not use. */
      readonly area: Area;
    }
  | {
      /** The name of the place the user may not use, already translated ("Invites"). */
      readonly place: string;
    };

/**
 * Names the area or the place in one sentence, says whom to ask and offers one action.
 *
 * @param props - The area or the place.
 * @returns The page.
 */
export function NotAvailable(props: NotAvailableProps): JSX.Element {
  const { t } = useTranslation();
  const name = "place" in props ? props.place : t(`areas.${props.area.id}.name`);
  return (
    <Page title={t("notAvailable.title", { area: name })}>
      <p>{t("notAvailable.body")}</p>
      <Link className="button" href="/">
        {t("notAvailable.action")}
      </Link>
    </Page>
  );
}
