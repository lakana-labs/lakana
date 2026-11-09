/**
 * The page of one area: its name, its purpose and, until its screen comes, the command line.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { ShippedArea } from "./areas";
import { ComingSoon } from "./ComingSoon";
import { Page } from "./Page";

/** Properties of {@link AreaPage}. */
export interface AreaPageProps {
  /** The area. */
  readonly area: ShippedArea;
  /** Whether to say that the person has no access yet (Home only). */
  readonly noAccessYet?: boolean;
}

/**
 * Renders an area's page.
 *
 * @param props - The area.
 * @param props.area - The area.
 * @param props.noAccessYet - Whether to say that the person has no access yet.
 * @returns The page.
 */
export function AreaPage({ area, noAccessYet = false }: AreaPageProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page title={t(`areas.${area.id}.name`)}>
      {noAccessYet && <p className="page-notice">{t("home.noAccessYet")}</p>}
      <p className="page-purpose">{t(`areas.${area.id}.purpose`)}</p>
      <ComingSoon part={area.part} cli={area.cli} />
    </Page>
  );
}
