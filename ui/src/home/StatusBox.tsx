/**
 * Home's status box: whether the instance works, in a glyph, a word and a sentence, and for
 * whoever may manage the instance its compute, memory and workers.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useId } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getStatus, type InstanceStatusOut, type StatusDetailOut } from "../api";
import { Loaded } from "../shell/Loaded";
import { StatusGlyph } from "../status/StatusGlyph";
import type { StatusKind } from "../status/status";
import { usePolled } from "./usePolled";

/** How often the box reads the status again: twice a minute. */
const EVERY_MS = 30_000;

const GIB = 2 ** 30;

/** The glyph of each state: the shapes of succeeded, warning and failed. */
const GLYPHS: Record<InstanceStatusOut["state"], StatusKind> = {
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
function sentences(t: TFunction, status: InstanceStatusOut): string[] {
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

/** Properties of {@link Figures}. */
interface FiguresProps {
  /** What the person sees beyond the state. */
  readonly detail: StatusDetailOut;
  /** The status, for the workers' counts. */
  readonly status: InstanceStatusOut;
}

/**
 * Compute and memory as two bars with their numbers, the workers by state, and the way to the
 * details.
 *
 * @param props - The detail and the status.
 * @param props.detail - What the person sees beyond the state.
 * @param props.status - The status.
 * @returns The figures.
 */
function Figures({ detail, status }: FiguresProps): JSX.Element {
  const { t } = useTranslation();
  const compute = useId();
  const memory = useId();
  const lost = status.workers_total - status.workers_alive;
  const limit = detail.memory_limit_bytes;
  return (
    <>
      <dl className="home-figures">
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
                <span className="home-number">
                  {t("home.status.slots", { busy: detail.slots_busy, total: detail.slots_total })}
                </span>
              </>
            ) : (
              <span className="home-number">{t("home.status.noSlots")}</span>
            )}
          </dd>
        </div>
        <div>
          <dt id={memory}>{t("home.status.memory")}</dt>
          <dd>
            {limit === null ? (
              <span className="home-number">
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
                <span className="home-number">
                  {t("home.status.memoryOf", {
                    used: gib(t, detail.memory_used_bytes),
                    limit: gib(t, limit),
                  })}
                </span>
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>{t("home.status.workers")}</dt>
          <dd>
            <span className="home-number">
              {lost > 0
                ? t("home.status.workersDown", { alive: status.workers_alive, lost })
                : t("home.status.workersAlive", { alive: status.workers_alive })}
            </span>
          </dd>
        </div>
      </dl>
      <Link className="home-more" to="/settings/status">
        {t("home.status.details")}
        <span aria-hidden="true"> ›</span>
      </Link>
    </>
  );
}

/**
 * Shows the status and reads it again every 30 seconds in the background, so the box never
 * keeps a session alive. The figures are drawn whenever the server answers them (only to a
 * person who may manage the instance), never from the person's role in the interface.
 *
 * @returns The box.
 */
export function StatusBox(): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  const status = usePolled(getStatus, EVERY_MS);
  return (
    <section className="home-section home-status" aria-labelledby={heading}>
      <h2 id={heading}>{t("home.status.title")}</h2>
      <Loaded resource={status}>
        {(found) => (
          <>
            <p className="home-state">
              <StatusGlyph kind={GLYPHS[found.state]} />
              <span>{t(`home.status.states.${found.state}`)}</span>
            </p>
            {sentences(t, found).map((sentence) => (
              <p className="home-sentence" key={sentence}>
                {sentence}
              </p>
            ))}
            {found.detail !== null && <Figures detail={found.detail} status={found} />}
          </>
        )}
      </Loaded>
    </section>
  );
}
