/**
 * An environment's usage on its page: its tables, dropped tables, artifacts and code archives,
 * each counted and sized, and the total.
 *
 * @packageDocumentation
 */
import { Fragment, type JSX, useCallback } from "react";
import { useTranslation } from "react-i18next";

import { type EnvironmentUsageOut, getUsage } from "../api/environments";
import { ReadFailed } from "../catalog/ReadFailed";
import { formatBytes } from "../catalog/format";
import { useResource } from "../shell/useResource";
import "./environmentPageStrings";
import "./environmentStrings";

const P = "settings.environmentPage";

/** The parts of a usage, in the order shown. */
const PARTS = ["tables", "dropped_tables", "artifacts", "code_archives"] as const;

/** Properties of {@link EnvironmentUsage}. */
export interface EnvironmentUsageProps {
  /** The environment's name. */
  readonly name: string;
}

/**
 * Renders the usage panel; while some sizes are not measured yet it says so, and the total is a
 * floor.
 *
 * @param props - The environment.
 * @param props.name - The environment's name.
 * @returns The panel.
 */
export function EnvironmentUsage({ name }: EnvironmentUsageProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const load = useCallback(() => getUsage(name), [name]);
  const read = useResource<EnvironmentUsageOut>(load);
  const usage = read.data;
  const size = (bytes: number): string => formatBytes(bytes, i18n.language);
  return (
    <section className="flat-panel settings-panel">
      <h2>{t(`${P}.usage`)}</h2>
      {usage === undefined ? (
        read.error === null ? (
          <p className="step-status" role="status">
            {t(`${P}.usageReading`)}
          </p>
        ) : (
          <ReadFailed message={read.error} onRetry={read.reload} />
        )
      ) : (
        <>
          <dl className="settings-facts">
            {PARTS.map((part) => (
              <Fragment key={part}>
                <dt>{t(`${P}.parts.${part}`)}</dt>
                <dd>
                  {t(`${P}.part`, { count: usage[part].count, size: size(usage[part].bytes) })}
                </dd>
              </Fragment>
            ))}
            <dt>{t(`${P}.total`)}</dt>
            <dd>
              {usage.complete
                ? size(usage.total_bytes)
                : t("settings.environmentPages.atLeast", { size: size(usage.total_bytes) })}
            </dd>
          </dl>
          {!usage.complete && <p className="page-purpose">{t(`${P}.incomplete`)}</p>}
        </>
      )}
    </section>
  );
}
