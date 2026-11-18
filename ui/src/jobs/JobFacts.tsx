/**
 * The facts of a job: where it is defined, its schedule, what it runs, after each deploy or not,
 * where it notifies, and why a start waits.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { JobOut, NotifyOut } from "../api/jobs";
import { HostLink } from "../catalog/table/SourceButton";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import "./jobStrings";
import { deploysPath, jobRunPath } from "./paths";
import { describeSchedule } from "./schedule";

/** Properties of {@link Fact}. */
export interface FactProps {
  /** The term. */
  readonly term: string;
  /** The answer. */
  readonly children: ReactNode;
}

/**
 * One term of a description list and its answer, which goes under the term on a narrow screen.
 *
 * @param props - The term and the answer.
 * @param props.term - The term.
 * @param props.children - The answer.
 * @returns The pair.
 */
export function Fact({ term, children }: FactProps): JSX.Element {
  return (
    <div className="jobs-fact">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** The events a job's `notify:` can name, which have words of their own. */
const EVENTS = ["failed", "recovered", "columns_changed", "health"];

/**
 * Where a job notifies, in words: the events, the format and, when the server sent it, the
 * secret that holds the address. An event a newer server names is shown as it is.
 *
 * @param props - The notification.
 * @param props.notify - Where the job reports.
 * @returns The sentence.
 */
function Notifies({ notify }: { readonly notify: NotifyOut }): JSX.Element {
  const { t } = useTranslation();
  const words = notify.events.map((event) =>
    EVENTS.includes(event) ? t(`jobs.facts.events.${event}`) : event,
  );
  const last = words.slice(-1).join("");
  const events =
    words.length > 1
      ? t("jobs.facts.listTwo", { first: words.slice(0, -1).join(", "), last })
      : last;
  const sentence = t("jobs.facts.notifiesOn", {
    events,
    format: t(`jobs.facts.formats.${notify.format}`),
  });
  return (
    <>
      {sentence}
      {notify.secret !== undefined && ` · ${t("jobs.facts.viaSecret", { secret: notify.secret })}`}
    </>
  );
}

/**
 * The job's schedule in words with its zone, then "paused" or the next run; or the sentence of a
 * job that has no schedule here. A cron the words do not cover is shown as it is, in monospace.
 *
 * @param props - The job.
 * @param props.job - The job.
 * @returns The schedule.
 */
function Schedule({ job }: { readonly job: JobOut }): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  if (job.cron === null || job.timezone === null) {
    return <>{t("jobs.facts.noSchedule")}</>;
  }
  const schedule = describeSchedule(job.cron, job.timezone, t);
  return (
    <>
      {schedule.verbatim ? (
        <span className="jobs-verbatim">
          <VisibleText text={schedule.text} />
        </span>
      ) : (
        <VisibleText text={schedule.text} />
      )}
      {job.paused
        ? ` · ${t("jobs.facts.paused")}`
        : job.next_run_at !== null &&
          ` · ${t("jobs.facts.nextRun", { when: when(job.next_run_at) })}`}
    </>
  );
}

/** Properties of {@link JobFacts}. */
export interface JobFactsProps {
  /** The job's environment. */
  readonly environment: string;
  /** The job. */
  readonly job: JobOut;
}

/**
 * Renders the facts in a flat panel: why a start waits (with the run it waits for), then the
 * repository with `lakana.yaml` at the deployed commit and the link to its deploys, the schedule, the selection, whether it
 * runs after each deploy, and where it notifies.
 *
 * @param props - The environment and the job.
 * @param props.environment - The job's environment.
 * @param props.job - The job.
 * @returns The panel.
 */
export function JobFacts({ environment, job }: JobFactsProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useId();
  const flags = [
    job.upstream ? ` ${t("jobs.facts.upstream")}` : "",
    job.downstream ? ` ${t("jobs.facts.downstream")}` : "",
  ].join("");
  return (
    <section className="flat-panel jobs-facts" aria-labelledby={heading}>
      <h2 className="jobs-panel-heading" id={heading}>
        {t("jobs.facts.heading")}
      </h2>
      {job.waiting_for !== null && (
        <p className="jobs-waiting">
          <VisibleText text={job.waiting_for.message} />
          {job.waits_for_run_id !== null && (
            <>
              {" "}
              <Link to={jobRunPath(environment, job.waits_for_run_id)}>
                {t("jobs.note.waitsFor")}
              </Link>
            </>
          )}
        </p>
      )}
      <dl>
        <Fact term={t("jobs.facts.repository")}>
          <VisibleText text={job.repository} />
          {" · "}
          {t("jobs.facts.definedAt", { commit: job.commit_sha.slice(0, 7) })}
          <HostLink url={job.source_url} label={t("jobs.facts.openHost")} before=" · " />
          {" · "}
          <Link to={deploysPath(environment, job.repository)}>
            {t("jobs.facts.deploys", { repository: job.repository })}
          </Link>
        </Fact>
        <Fact term={t("jobs.facts.schedule")}>
          <Schedule job={job} />
        </Fact>
        <Fact term={t("jobs.facts.runs")}>
          {t("jobs.facts.select", { patterns: job.select.join(" ") })}
          {flags}
        </Fact>
        <Fact term={t("jobs.facts.afterDeploy")}>
          {t(
            job.triggers.some((trigger) => trigger.kind === "deploy")
              ? "jobs.facts.yes"
              : "jobs.facts.no",
          )}
        </Fact>
        <Fact term={t("jobs.facts.notifies")}>
          {job.notify === null ? t("jobs.facts.noNotify") : <Notifies notify={job.notify} />}
        </Fact>
      </dl>
    </section>
  );
}
