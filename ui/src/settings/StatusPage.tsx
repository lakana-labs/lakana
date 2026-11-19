/**
 * Settings › Status: the instance's health in full, its workers and the version it runs; for
 * whoever may see the detail, compute and memory, every worker and the scheduler.
 *
 * @packageDocumentation
 */
import { type JSX, useRef } from "react";
import { useTranslation } from "react-i18next";

import { getScheduler, getStatus, type InstanceStatusOut } from "../api";
import { ReadFailed } from "../catalog/ReadFailed";
import { CopyLine } from "../shell/CopyLine";
import { useInterfaceState } from "../shell/InterfaceState";
import { Loaded } from "../shell/Loaded";
import { useLivePoll } from "../shell/useLivePoll";
import { useWhen } from "../shell/useWhen";
import { markedText, VisibleText } from "../shell/VisibleText";
import { InstanceState, StatusFigures, statusSentences } from "../status/instanceStatus";
import { StatusGlyph } from "../status/StatusGlyph";
import { SettingsFrame } from "./SettingsFrame";
import { StatusWorkers } from "./StatusWorkers";
import "./statusStrings";

/** How often the page reads the status again: twice a minute, as Home does. */
const EVERY_MS = 30_000;

/** A status with the time it was read, for the line that says how old a kept answer is. */
interface Read {
  /** The status. */
  readonly status: InstanceStatusOut;
  /** When it arrived. */
  readonly at: string;
}

/**
 * Reads the status and notes when it arrived.
 *
 * @returns The status and the time.
 */
async function readStatus(): Promise<Read> {
  return { status: await getStatus(), at: new Date().toISOString() };
}

/** Properties of {@link Health}. */
interface HealthProps {
  /** The status. */
  readonly status: InstanceStatusOut;
}

/**
 * The state, its sentences, and how many workers are up and lost.
 *
 * @param props - The status.
 * @param props.status - The status.
 * @returns The section's content.
 */
function Health({ status }: HealthProps): JSX.Element {
  const { t } = useTranslation();
  const lost = status.workers_total - status.workers_alive;
  return (
    <>
      <InstanceState state={status.state} />
      {statusSentences(t, status).map((sentence) => (
        <p className="instance-sentence" key={sentence}>
          {sentence}
        </p>
      ))}
      {status.workers_total === 0 ? (
        <>
          <p>{t("settings.statusPage.noWorker")}</p>
          <CopyLine
            text={t("settings.statusPage.workerCommand")}
            copyLabel={t("settings.statusPage.copyCommand")}
          />
        </>
      ) : (
        <p>
          {lost > 0
            ? t("settings.statusPage.workersLost", { count: status.workers_alive, lost })
            : t("settings.statusPage.workersUp", { count: status.workers_alive })}
        </p>
      )}
    </>
  );
}

/**
 * The scheduler: which server holds it, its last heartbeat and how long its queues are. Read
 * only while the status answers the detail, which the scheduler's route allows the same people.
 *
 * @returns The section's content.
 */
function Scheduler(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const scheduler = useLivePoll(getScheduler, { everyMs: EVERY_MS, active: true });
  return (
    <Loaded resource={scheduler}>
      {(found) => (
        <dl className="instance-figures">
          <div>
            <dt>{t("settings.statusPage.holder")}</dt>
            <dd>
              {found.holder === null ? (
                <span className="status-word">
                  <StatusGlyph kind="failed" />
                  {t("settings.statusPage.noHolder")}
                </span>
              ) : (
                <VisibleText text={found.holder} />
              )}
            </dd>
          </div>
          <div>
            <dt>{t("settings.statusPage.heartbeat")}</dt>
            <dd>
              {found.heartbeat_at === null
                ? t("settings.statusPage.never")
                : when(found.heartbeat_at)}
            </dd>
          </div>
          {(["activeRuns", "pending", "ready", "running"] as const).map((key) => (
            <div key={key}>
              <dt>{t(`settings.statusPage.${key}`)}</dt>
              <dd className="instance-number">
                {found[key === "activeRuns" ? "active_runs" : key]}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </Loaded>
  );
}

/**
 * Renders Status and reads it again every 30 seconds in the background, as Home does. A failed
 * read keeps the last answer, with a line that says how old it is and Try again.
 *
 * @returns The place.
 */
export function StatusPage(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const { version } = useInterfaceState();
  const heading = useRef<HTMLHeadingElement>(null);
  const read = useLivePoll(readStatus, { everyMs: EVERY_MS, active: true });
  const retry = (): void => {
    heading.current?.focus();
    read.reload();
  };
  const found = read.data;
  const detail = found?.status.detail ?? null;
  return (
    <SettingsFrame title={t("settings.places.status")} headingRef={heading}>
      <p className="page-purpose">{t("settings.statusPage.intro")}</p>
      {read.error !== null && (
        <ReadFailed
          message={
            found === undefined
              ? read.error
              : t("settings.statusPage.stale", { when: when(found.at) })
          }
          onRetry={retry}
        />
      )}
      {found === undefined && read.error === null && (
        <p className="step-status" role="status">
          {t("session.loading")}
        </p>
      )}
      {found !== undefined && (
        <section className="flat-panel settings-panel">
          <h2>{t("settings.statusPage.health")}</h2>
          <Health status={found.status} />
        </section>
      )}
      {found !== undefined && detail !== null && (
        <>
          <section className="flat-panel settings-panel">
            <h2>{t("settings.statusPage.computeAndMemory")}</h2>
            <StatusFigures detail={detail} status={found.status} workers={false} />
          </section>
          <section className="flat-panel settings-panel">
            <h2>{t("settings.statusPage.workers")}</h2>
            <StatusWorkers workers={detail.workers} version={version} />
          </section>
          <section className="flat-panel settings-panel">
            <h2>{t("settings.statusPage.scheduler")}</h2>
            <Scheduler />
          </section>
        </>
      )}
      <section className="flat-panel settings-panel">
        <h2>{t("settings.statusPage.version")}</h2>
        <p>
          {version === null
            ? t("settings.statusPage.development")
            : t("settings.statusPage.runs", { version: markedText(version) })}
        </p>
        <div data-slot="version-notice" />
      </section>
    </SettingsFrame>
  );
}
