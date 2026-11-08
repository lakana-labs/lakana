/**
 * The state of an area the user may not use: a deep link to it lands here.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Link } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { Area } from "./areas";
import { Page } from "./Page";

/** Properties of {@link NotAvailable}. */
export interface NotAvailableProps {
  /** The area the user may not use. */
  readonly area: Area;
}

/**
 * Names the area in one sentence, says whom to ask and offers one action.
 *
 * @param props - The area.
 * @param props.area - The area the user may not use.
 * @returns The page.
 */
export function NotAvailable({ area }: NotAvailableProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page title={t("notAvailable.title", { area: t(`areas.${area.id}.name`) })}>
      <p>{t("notAvailable.body")}</p>
      <Link className="button" href="/">
        {t("notAvailable.action")}
      </Link>
    </Page>
  );
}
