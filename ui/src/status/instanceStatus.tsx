/**
 * The instance's state as Home's status box and Settings › Status both draw it: the glyph and
 * word, the sentences, and compute and memory as two bars with their numbers.
 *
 * @remarks
 * Its strings stay under `home.status` in `en.json`, since Home's first load shows them.
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useId } from "react";
import { useTranslation } from "react-i18next";

import type { InstanceStatusOut, StatusDetailOut } from "../api";
import { StatusGlyph } from "./StatusGlyph";
import "./instanceStatus.css";
import type { StatusKind } from "./status";

const GIB = 2 ** 30;

/** The glyph of each state: the shapes of succeeded, warning and failed. */
export const INSTANCE_GLYPHS: Record<InstanceStatusOut["state"], StatusKind> = {
  healthy: "succeeded",
  degraded: "warning",
  down: "failed",
};

/**
 * A number of bytes in GiB with one decimal, the unit the workers' limit is set in.
 *
 * @param t - The translation function.
 * @param bytes - The number of bytes.
 * @returns The words, such as "1.2 GiB".
 */
function gib(t: TFunction, bytes: number): string {
  return t("home.status.gib", { value: (bytes / GIB).toFixed(1) });
}

/**
 * The sentences for the reader: one per cause they may see, or one for the state.
 *
 * @param t - The translation function.
 * @param status - The status.
 * @returns The sentences.
 */
export function statusSentences(t: TFunction, status: InstanceStatusOut): string[] {
  if (status.causes.length === 0) {
    return [t(`home.status.without.${status.state}`)];
  }
  return status.causes.map((cause) =>
    cause === "worker_lost"
      ? t("home.status.causes.worker_lost", {
          count: status.workers_total - status.workers_alive,
          total: status.workers_total,
        })
      : t(`home.status.causes.${cause}`),
  );
}

/** Properties of {@link InstanceState}. */
export interface InstanceStateProps {
  /** The instance's state. */
  readonly state: InstanceStatusOut["state"];
}

/**
 * The state as glyph and word.
 *
 * @param props - The state.
 * @param props.state - The instance's state.
 * @returns The line.
 */
export function InstanceState({ state }: InstanceStateProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <p className="instance-state">
      <StatusGlyph kind={INSTANCE_GLYPHS[state]} />
      <span>{t(`home.status.states.${state}`)}</span>
    </p>
  );
}

/** Properties of {@link StatusFigures}. */
export interface StatusFiguresProps {
  /** What the person sees beyond the state. */
  readonly detail: StatusDetailOut;
  /** The status, for the workers' counts. */
  readonly status: InstanceStatusOut;
  /** Whether the workers' counts are a row of their own; Status names them elsewhere. */
  readonly workers?: boolean;
}

/**
 * Compute and memory as two bars with their numbers, and the workers by state.
 *
 * @param props - The detail and the status.
 * @param props.detail - What the person sees beyond the state.
 * @param props.status - The status.
 * @param props.workers - Whether the workers' counts are a row of their own; yes by default.
 * @returns The figures.
 */
export function StatusFigures({ detail, status, workers = true }: StatusFiguresProps): JSX.Element {
  const { t } = useTranslation();
  const compute = useId();
  const memory = useId();
  const lost = status.workers_total - status.workers_alive;
  const limit = detail.memory_limit_bytes;
  return (
    <dl className="instance-figures">
      <div>
        <dt id={compute}>{t("home.status.compute")}</dt>
        <dd>
          {detail.slots_total > 0 ? (
            <>
              <meter
                aria-labelledby={compute}
                min={0}
                max={detail.slots_total}
                value={detail.slots_busy}
              />
              <span className="instance-number">
                {t("home.status.slots", { busy: detail.slots_busy, total: detail.slots_total })}
              </span>
            </>
          ) : (
            <span className="instance-number">{t("home.status.noSlots")}</span>
          )}
        </dd>
      </div>
      <div>
        <dt id={memory}>{t("home.status.memory")}</dt>
        <dd>
          {limit === null ? (
            <span className="instance-number">
              {t("home.status.memoryNoLimit", { used: gib(t, detail.memory_used_bytes) })}
            </span>
          ) : (
            <>
              <meter
                aria-labelledby={memory}
                min={0}
                max={limit}
                value={detail.memory_used_bytes}
              />
              <span className="instance-number">
                {t("home.status.memoryOf", {
                  used: gib(t, detail.memory_used_bytes),
                  limit: gib(t, limit),
                })}
              </span>
            </>
          )}
        </dd>
      </div>
      {workers && (
        <div>
          <dt>{t("home.status.workers")}</dt>
          <dd>
            <span className="instance-number">
              {lost > 0
                ? t("home.status.workersDown", { alive: status.workers_alive, lost })
                : t("home.status.workersAlive", { alive: status.workers_alive })}
            </span>
          </dd>
        </div>
      )}
    </dl>
  );
}
