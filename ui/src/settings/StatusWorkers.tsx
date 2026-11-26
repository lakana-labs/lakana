/**
 * Settings › Status: every registered worker, one static row each, lost ones first.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { StatusWorkerOut } from "../api";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { StatusGlyph } from "../status/StatusGlyph";
import "./statusStrings";

/**
 * A number of bytes in GiB, without the unit; the message writes it with one decimal.
 *
 * @param bytes - The number of bytes.
 * @returns The number, such as 1.2.
 */
const inGib = (bytes: number): number => bytes / 2 ** 30;

/** The word for the work a worker takes, by its code. */
const TAKES = {
  all: "settings.statusPage.takesAll",
  interactive: "settings.statusPage.takesInteractive",
  pipeline: "settings.statusPage.takesPipeline",
} as const;

/**
 * What a worker is doing: idle, or the kind of its run.
 *
 * @param t - The translation function.
 * @param worker - The worker.
 * @returns The words.
 */
function doing(t: TFunction, worker: StatusWorkerOut): string {
  if (worker.query_run_id !== null) return t("settings.statusPage.query");
  if (worker.deploy_run_id !== null) return t("settings.statusPage.deploy");
  if (worker.task_run_id !== null) return t("settings.statusPage.task");
  return t("settings.statusPage.idle");
}

/**
 * The memory a worker last reported, against its limit.
 *
 * @param t - The translation function.
 * @param worker - The worker.
 * @returns The words; a dash before it reports any.
 */
function memoryText(t: TFunction, worker: StatusWorkerOut): string {
  const memory = worker.memory ?? null;
  if (memory === null) return t("settings.statusPage.notReported");
  const used = inGib(memory.used_bytes);
  return memory.limit_bytes === null
    ? t("settings.statusPage.memoryNoLimit", { used })
    : t("settings.statusPage.memoryOf", { used, limit: inGib(memory.limit_bytes) });
}

/**
 * The workers, lost first, then by host.
 *
 * @param workers - The workers as the server lists them.
 * @returns A sorted copy.
 */
function sorted(workers: readonly StatusWorkerOut[]): StatusWorkerOut[] {
  return [...workers].sort(
    (a, b) =>
      Number(b.status === "lost") - Number(a.status === "lost") || a.host.localeCompare(b.host),
  );
}

/** Properties of {@link StatusWorkers}. */
export interface StatusWorkersProps {
  /** Every registered worker. */
  readonly workers: readonly StatusWorkerOut[];
  /** The server's version, to tell a worker that runs another; null when unknown. */
  readonly version: string | null;
}

/**
 * The workers in their own scrolling region: host, state, what they take and do, memory,
 * version and when they were last seen. A worker has no page, so the rows open nothing.
 *
 * @param props - The workers and the server's version.
 * @param props.workers - Every registered worker.
 * @param props.version - The server's version.
 * @returns The list, or the sentence that there is none.
 */
export function StatusWorkers({ workers, version }: StatusWorkersProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  if (workers.length === 0) {
    return <p>{t("settings.statusPage.noWorkerRow")}</p>;
  }
  return (
    <div
      className="list-scroll status-workers"
      role="region"
      aria-label={t("settings.statusPage.workersRegion")}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list list-static">
        <thead>
          <tr>
            <th scope="col">{t("settings.statusPage.host")}</th>
            <th scope="col">{t("settings.statusPage.state")}</th>
            <th scope="col">{t("settings.statusPage.takes")}</th>
            <th scope="col">{t("settings.statusPage.doing")}</th>
            <th scope="col" className="number">
              {t("settings.statusPage.memory")}
            </th>
            <th scope="col">{t("settings.statusPage.version")}</th>
            <th scope="col">{t("settings.statusPage.lastSeen")}</th>
          </tr>
        </thead>
        <tbody>
          {sorted(workers).map((worker) => {
            const lost = worker.status === "lost";
            return (
              <tr key={worker.id}>
                <td>
                  <span className="row-name">
                    <VisibleText text={worker.host} />
                  </span>
                </td>
                <td>
                  <span className="status-word">
                    <StatusGlyph kind={lost ? "failed" : "succeeded"} />
                    {t(
                      lost
                        ? worker.replaced
                          ? "settings.statusPage.lostReplaced"
                          : "settings.statusPage.lost"
                        : "settings.statusPage.alive",
                    )}
                  </span>
                </td>
                <td>{t(TAKES[worker.takes])}</td>
                <td>{doing(t, worker)}</td>
                <td className="number">{memoryText(t, worker)}</td>
                <td>
                  <VisibleText text={worker.version} />
                  {version !== null && worker.version !== version && (
                    <span className="status-differs"> {t("settings.statusPage.differs")}</span>
                  )}
                </td>
                <td>{when(worker.heartbeat_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
