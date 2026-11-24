/**
 * The Runs tab of the Jobs area: every run of the environments the person may read, newest
 * first, filtered by environment, state, trigger, job and period.
 *
 * @packageDocumentation
 */
import { type JSX, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { Button, Input, Label, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { RequestOptions } from "../api/client";
import type { EnvironmentChoice } from "../api/environments";
import { type JobOut, listAllRuns, type RunOut } from "../api/jobs";
import type { CursorPage } from "../api/manage";
import { ViewList } from "../catalog/views/ViewList";
import { ChoiceMenu } from "../shell/ChoiceMenu";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { Environments } from "./Environments";
import { PERIOD_CHOICES } from "./period";
import { RunRow } from "./RunRow";
import "./runStrings";
import { STATE_CHOICES, toAllRunsFilter, TRIGGER_CHOICES, useRunsFilter } from "./useRunsFilter";
import { useRunPages } from "./useRunPages";
import { readWaitingStarts, WaitingRow } from "./WaitingRow";

/** A page of runs; the first one also holds the starts that wait, read at the same moment. */
interface RunsPage extends CursorPage<RunOut> {
  /** The starts that wait, newest request first; empty on a later page. */
  readonly waiting: readonly JobOut[];
}

/** Properties of {@link JobField}. */
interface JobFieldProps {
  /** The job's name in the address; empty for every job. */
  readonly job: string;
  /** Called with the name typed, on Enter or when the field is left. */
  readonly onApply: (job: string) => void;
  /** The field, which takes the focus after Clear filters. */
  readonly inputRef: RefObject<HTMLInputElement | null>;
}

/**
 * The field "Job": an exact job name, applied on Enter or when the field is left, so a name
 * typed is one step of the history, not one per key. A name the address brings from elsewhere
 * (Back, Forward, Clear filters) replaces the one typed.
 *
 * @param props - The name and what to do with it.
 * @param props.job - The name in the address.
 * @param props.onApply - Called with the name typed.
 * @param props.inputRef - The field's input.
 * @returns The field.
 */
function JobField({ job, onApply, inputRef }: JobFieldProps): JSX.Element {
  const { t } = useTranslation();
  const [typed, setTyped] = useState(job);
  const [seen, setSeen] = useState(job);
  if (seen !== job) {
    setSeen(job);
    setTyped(job);
  }
  const apply = (): void => {
    if (typed.trim() !== job) {
      // The name applied is the one the field follows from now on: an address that leaves it
      // (Back before it was ever shown) puts its own name in the field.
      setSeen(typed.trim());
      onApply(typed);
    }
  };
  return (
    <TextField className="field jobs-filter jobs-job-field" value={typed} onChange={setTyped}>
      <Label>{t("jobs.runsTab.job")}</Label>
      <Input
        ref={inputRef}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            apply();
          }
        }}
        onBlur={apply}
      />
    </TextField>
  );
}

/** Properties of {@link RunsList}. */
interface RunsListProps {
  /** The environments the person may read; at least one. */
  readonly environments: readonly EnvironmentChoice[];
}

/**
 * The filters, then the runs: 50 a page with Show more, the first page read again every 30 s
 * until Show more read a further page; then Read again starts over from the first page. Above
 * the runs, the starts that wait for the run holding their project, read from the environments'
 * jobs with the first page (and with it on Refresh and Read again, and kept as they were while
 * Show more holds the list), held against the same filters.
 *
 * @param props - The environments.
 * @param props.environments - The environments the person may read.
 * @returns The filters and the list.
 */
function RunsList({ environments }: RunsListProps): JSX.Element {
  const { t } = useTranslation();
  const filter = useRunsFilter(environments);
  const { choice } = filter;
  const jobInput = useRef<HTMLInputElement>(null);
  const key = JSON.stringify(choice);
  const { environment, status, trigger, job, period } = choice;
  const names = JSON.stringify(environments.map((choice) => choice.name));
  const load = useCallback(
    async (cursor: string | null, options: RequestOptions): Promise<RunsPage> => {
      const filtered = toAllRunsFilter({ environment, status, trigger, job, period });
      const runs = listAllRuns(filtered, cursor, { background: options.background === true });
      if (cursor !== null) {
        return { ...(await runs), waiting: [] };
      }
      const listed = environment === null ? (JSON.parse(names) as string[]) : [environment];
      const [page, waiting] = await Promise.all([
        runs,
        readWaitingStarts({ environments: listed, status, trigger, job, period }),
      ]);
      return { ...page, waiting };
    },
    [environment, status, trigger, job, period, names],
  );
  const pages = useRunPages(key, load, 0);
  const rows = pages.items ?? [];
  const waiting = pages.firstPage?.waiting ?? [];
  // Read again takes its own button away: the focus goes to the line that says the list is read
  // again, then to the list, or to what stands in its place, never to the page.
  const readingAgain = useRef<HTMLParagraphElement>(null);
  const region = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const wasRestarting = useRef(false);
  const { restarting } = pages;
  useEffect(() => {
    if (restarting) {
      readingAgain.current?.focus();
    } else if (wasRestarting.current && document.activeElement === document.body) {
      // The list, or what stands in its place: Try again after a failed read, or the sentence
      // that no runs match.
      const place =
        region.current ?? frame.current?.querySelector<HTMLElement>(".read-failed button, .empty");
      place?.focus();
    }
    wasRestarting.current = restarting;
  }, [restarting]);
  const loaded = pages.items !== undefined && !pages.busy;
  return (
    <>
      <div className="jobs-find">
        <EnvironmentPicker
          environments={environments}
          value={choice.environment}
          label={t("jobs.list.environment")}
          all={t("jobs.list.allEnvironments")}
          onChange={(chosen: string | null) => {
            filter.set({ environment: chosen });
          }}
        />
        <ChoiceMenu
          className="jobs-filter"
          label={t("jobs.runsTab.state")}
          choices={STATE_CHOICES.map((id) => ({ id, text: t(`jobs.runsTab.states.${id}`) }))}
          selected={choice.status}
          onChoose={(status) => {
            filter.set({ status });
          }}
        />
        <ChoiceMenu
          className="jobs-filter"
          label={t("jobs.runsTab.trigger")}
          choices={TRIGGER_CHOICES.map((id) => ({ id, text: t(`jobs.runsTab.triggers.${id}`) }))}
          selected={choice.trigger}
          onChoose={(trigger) => {
            filter.set({ trigger });
          }}
        />
        <JobField
          job={choice.job}
          inputRef={jobInput}
          onApply={(job) => {
            filter.set({ job });
          }}
        />
        <ChoiceMenu
          className="jobs-filter"
          label={t("jobs.runsTab.period")}
          choices={PERIOD_CHOICES.map((id) => ({ id, text: t(`jobs.runs.periods.${id}`) }))}
          selected={choice.period}
          onChoose={(period) => {
            filter.set({ period });
          }}
        />
      </div>
      {pages.restarting && (
        <p className="jobs-reading" role="status" tabIndex={-1} ref={readingAgain}>
          {t("jobs.runsTab.readingAgain")}
        </p>
      )}
      {pages.shown && (
        <div className="jobs-again">
          <p>{t("jobs.runsTab.stopped")}</p>
          <Button className="button" onPress={pages.restart}>
            {t("jobs.runsTab.readAgain")}
          </Button>
        </div>
      )}
      {/* Each filter's list starts afresh: no row of the previous filter stays while its own first
          page is read, and a failed first read says so at once. */}
      <div ref={frame}>
        <ViewList
          key={key}
          pages={pages}
          rowCount={pages.items === undefined ? 0 : waiting.length + rows.length}
          empty={t("jobs.runsTab.empty")}
          region={t("jobs.runsTab.region")}
          rowsOpen
          scroll="jobs-tab-runs-scroll"
          toolbar={{ count: "jobs.runsTab" }}
          regionRef={region}
          head={
            <tr>
              <th scope="col">{t("jobs.runsTab.columns.state")}</th>
              <th scope="col">{t("jobs.runsTab.columns.run")}</th>
              <th scope="col">{t("jobs.runsTab.columns.environment")}</th>
              <th scope="col">{t("jobs.runsTab.columns.startedBy")}</th>
              <th scope="col">{t("jobs.runsTab.columns.created")}</th>
              <th scope="col" className="number">
                {t("jobs.runsTab.columns.duration")}
              </th>
              <th scope="col">{t("jobs.runsTab.columns.tasks")}</th>
              <th scope="col">
                <span className="visually-hidden">{t("list.open")}</span>
              </th>
            </tr>
          }
        >
          {pages.items !== undefined &&
            waiting.map((start) => (
              <WaitingRow
                key={JSON.stringify(["waiting", start.environment, start.repository, start.name])}
                job={start}
              />
            ))}
          {rows.map((run) => (
            <RunRow key={run.id} run={run} />
          ))}
        </ViewList>
      </div>
      {loaded && rows.length === 0 && !pages.hasMore && filter.narrowed && (
        <Button
          className="button"
          onPress={() => {
            filter.clear();
            jobInput.current?.focus();
          }}
        >
          {t("jobs.runsTab.clear")}
        </Button>
      )}
    </>
  );
}

/**
 * Renders the Runs tab: the environments the person may read, then the filters and the runs. It
 * shows runs started by hand for a selection of assets, and starts none.
 *
 * @returns The tab.
 */
export function RunsTab(): JSX.Element {
  return <Environments>{(listed) => <RunsList environments={listed} />}</Environments>;
}
