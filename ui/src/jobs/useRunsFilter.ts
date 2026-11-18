/**
 * What the Runs tab shows: the environment, the states, what started the runs, the job and the
 * period, all in the address.
 *
 * @packageDocumentation
 */
import { useSearchParams } from "react-router";

import type { EnvironmentChoice } from "../api/environments";
import type { AllRunsFilter, RunStatus } from "../api/jobs";
import { DEFAULT_PERIOD, type Period, readPeriod, sinceOf } from "./period";
import { useJobsFilter } from "./useJobsFilter";

/** The choices of the State menu, each the address's `status`; `all` is no narrowing. */
export const STATE_CHOICES = ["all", "queued,running", "succeeded", "failed", "cancelled"] as const;

/** A choice of the State menu. */
export type StateChoice = (typeof STATE_CHOICES)[number];

/** The choices of the Trigger menu, each the address's `trigger`; `all` is no narrowing. */
export const TRIGGER_CHOICES = ["all", "schedule", "manual", "api", "deploy"] as const;

/** A choice of the Trigger menu. */
export type TriggerChoice = (typeof TRIGGER_CHOICES)[number];

/** What the Runs tab is filtered by, as the address holds it. */
export interface RunsChoice {
  /** The environment; null for every environment the person may read. */
  readonly environment: string | null;
  /** The states. */
  readonly status: StateChoice;
  /** What started the runs. */
  readonly trigger: TriggerChoice;
  /** The job's exact name; empty for every job. */
  readonly job: string;
  /** How far back the runs go. */
  readonly period: Period;
}

/** What {@link useRunsFilter} hands back. */
export interface RunsFilter {
  /** The filter. */
  readonly choice: RunsChoice;
  /** Changes some of the filter; the address keeps it, this browser the environment. */
  readonly set: (change: Partial<RunsChoice>) => void;
  /** Clears every filter but the environment. */
  readonly clear: () => void;
  /** Whether any filter but the environment differs from its default. */
  readonly narrowed: boolean;
}

/**
 * Reads a menu's choice from the address; anything it does not know is `all`.
 *
 * @template T - The choices.
 * @param choices - The menu's choices.
 * @param value - The address's value, or null.
 * @returns The choice.
 */
function readChoice<T extends string>(choices: readonly T[], value: string | null): T | "all" {
  return choices.find((choice) => choice === value) ?? "all";
}

/**
 * The Runs tab's filter. The environment is the Jobs list's (`useJobsFilter`: the address, else
 * the one this browser remembers); the states (`?status=queued,running`), the trigger, the job
 * and the period (the last 30 days by default) are the address's. A change replaces the address
 * in the history, so Back leaves the tab with its filter.
 *
 * @param environments - The environments the person may read.
 * @returns The filter and how to change it.
 */
export function useRunsFilter(environments: readonly EnvironmentChoice[]): RunsFilter {
  const [params, setParams] = useSearchParams();
  const jobs = useJobsFilter(environments);
  const choice: RunsChoice = {
    environment: jobs.environment,
    status: readChoice(STATE_CHOICES, params.get("status")),
    trigger: readChoice(TRIGGER_CHOICES, params.get("trigger")),
    job: params.get("job")?.trim() ?? "",
    period: readPeriod(params.get("period")),
  };
  const write = (change: Partial<Omit<RunsChoice, "environment">>): void => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        const values: Record<string, string | undefined> = {
          status: change.status,
          trigger: change.trigger,
          job: change.job?.trim(),
          period: change.period,
        };
        const defaults: Record<string, string> = {
          status: "all",
          trigger: "all",
          job: "",
          period: DEFAULT_PERIOD,
        };
        for (const [name, value] of Object.entries(values)) {
          if (value === undefined) {
            continue;
          }
          if (value === defaults[name]) {
            next.delete(name);
          } else {
            next.set(name, value);
          }
        }
        return next;
      },
      { replace: true },
    );
  };
  return {
    choice,
    set: ({ environment, ...rest }) => {
      if (environment !== undefined) {
        jobs.setEnvironment(environment);
      }
      if (Object.keys(rest).length > 0) {
        write(rest);
      }
    },
    clear: () => {
      write({ status: "all", trigger: "all", job: "", period: DEFAULT_PERIOD });
    },
    narrowed:
      choice.status !== "all" ||
      choice.trigger !== "all" ||
      choice.job !== "" ||
      choice.period !== DEFAULT_PERIOD,
  };
}

/**
 * The filter the server reads for a choice; the period counts back from now.
 *
 * @param choice - The tab's filter.
 * @returns The filter of `listAllRuns`.
 */
export function toAllRunsFilter(choice: RunsChoice): AllRunsFilter {
  return {
    environment: choice.environment,
    status: choice.status === "all" ? [] : (choice.status.split(",") as RunStatus[]),
    trigger: choice.trigger === "all" ? [] : [choice.trigger],
    job: choice.job === "" ? null : choice.job,
    repository: null,
    since: sinceOf(choice.period),
  };
}
