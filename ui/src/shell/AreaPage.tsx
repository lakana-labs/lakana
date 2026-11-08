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
}

/**
 * Renders an area's page.
 *
 * @param props - The area.
 * @param props.area - The area.
 * @returns The page.
 */
export function AreaPage({ area }: AreaPageProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <Page title={t(`areas.${area.id}.name`)}>
      <p className="page-purpose">{t(`areas.${area.id}.purpose`)}</p>
      <ComingSoon part={area.part} cli={area.cli} />
    </Page>
  );
}
