/**
 * One query run per page: submitting it, following its state, cancelling it on request and
 * reading its result, a page of 100 rows at a time.
 *
 * @packageDocumentation
 */
import { type Dispatch, type SetStateAction, useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { ACTIVE_STATUSES, ApiError, cancelQuery, recordOpened } from "../api";
import type { ResultPageOut } from "../api";
import { getQueryRun, getResultPageAt, type QueryRunOut, submitQuery } from "../api/queries";
import { useMe } from "../shell/me";
import { runPath } from "./paths";

/** How often to poll a run at first. */
export const POLL_FAST_MS = 500;
/** For how long to poll at {@link POLL_FAST_MS}. */
export const POLL_FAST_FOR_MS = 5_000;
/** How often to poll after that. */
export const POLL_SLOW_MS = 2_000;
/** How long to wait after a poll failed. */
export const POLL_RETRY_MS = 5_000;
/** How many polls may fail in a row before the screen stops and offers Try again. */
export const POLL_FAILURES = 3;

/** The rows of one page of the result. */
const PAGE_ROWS = 100;

/**
 * Where a run is: none yet, being submitted, followed, its first page being read, ended (with
 * its first page when it succeeded and the result is kept), lost (its state or page could not be
 * read), or refused at the submit.
 */
type QueryRunPhase =
  "idle" | "submitting" | "waiting" | "loading_page" | "done" | "failed" | "refused";

/** What the screen shows of a run. */
export interface QueryRunState {
  /** Where the run is. */
  readonly phase: QueryRunPhase;
  /** The run's latest state; null before the first answer. */
  readonly run: QueryRunOut | null;
  /** The rows read so far; null until the first page arrives, or when the result is not kept. */
  readonly page: ResultPageOut | null;
  /** Why the submit was refused or the run was lost; null otherwise. */
  readonly error: unknown;
}

/** The run of the page and what can be done with it. */
export interface QueryRun extends QueryRunState {
  /** Whether a cancel was sent and the run has not ended yet. */
  readonly cancelling: boolean;
  /**
   * Why the last cancel was refused (a busy server, no connection); null otherwise. A run that
   * had already ended is no refusal: the next poll says how it ended.
   */
  readonly cancelRefusal: unknown;
  /** Whether the run was opened from its address rather than submitted on this page. */
  readonly opened: boolean;
  /**
   * Submits a query, moves to its run's address (keeping the focus where it is) and follows it.
   * A refusal ends in the phase `refused`, never in a rejection. `state` goes with the move, in
   * the history entry of the run's address.
   */
  readonly submit: (environment: string, sql: string, state?: object) => Promise<void>;
  /** Cancels the run while it is queued or running; true when there was one to cancel. */
  readonly cancel: () => boolean;
  /** Follows the run an address names, unless it is the one followed already. */
  readonly open: (environment: string, id: string) => void;
  /** Follows the run again after it was lost. */
  readonly retry: () => void;
  /**
   * Stops following the run, without a cancel, and shows none: an address that names no run
   * (a new query, a saved one) shows no run's result.
   */
  readonly reset: () => void;
  /** Reads the next page and appends its rows; rejects with the refusal. */
  readonly more: () => Promise<void>;
}

/** The view of a run, besides what can be done with it. */
type View = Omit<QueryRun, "submit" | "cancel" | "open" | "retry" | "more" | "reset">;

const IDLE: View = {
  phase: "idle",
  run: null,
  page: null,
  error: null,
  cancelling: false,
  cancelRefusal: null,
  opened: false,
};

/** A run, by its environment and id. */
interface Target {
  /** The environment. */
  readonly environment: string;
  /** The run's id. */
  readonly id: string;
}

/**
 * Ignores a failed record: Recently opened is a convenience.
 *
 * @returns Nothing.
 */
function ignore(): undefined {
  return undefined;
}

/**
 * The rows shown with the next page's appended.
 *
 * @param shown - The rows shown.
 * @param next - The next page.
 * @returns One page with both, whose next offset is the next page's.
 */
function appendPage(shown: ResultPageOut, next: ResultPageOut): ResultPageOut {
  return {
    ...shown,
    rows: [...shown.rows, ...next.rows],
    row_cut: [...shown.row_cut, ...next.row_cut.map((at) => at + shown.rows.length)],
    columns_cut: Math.max(shown.columns_cut, next.columns_cut),
    next_offset: next.next_offset,
  };
}

/**
 * Follows one run at a time. Every follow has a round; an answer of an older round is dropped,
 * so a new run, an opened one or leaving the page never shows an old answer.
 */
class Follower {
  /** The run followed; null before one, and after leaving the page. */
  #target: Target | null = null;
  /** The run while it may be cancelled; null otherwise. */
  #active: Target | null = null;
  /** Whether the cancel of the active run was sent. */
  #cancelSent = false;
  /** The rows shown and the run they belong to. */
  #shown: { readonly target: Target; readonly page: ResultPageOut } | null = null;
  #round = 0;
  #timer: ReturnType<typeof setTimeout> | undefined = undefined;
  readonly #show: Dispatch<SetStateAction<View>>;

  /**
   * Builds the follower.
   *
   * @param show - Sets the view of the run.
   */
  constructor(show: Dispatch<SetStateAction<View>>) {
    this.#show = show;
  }

  /** Stops following: no more polls, and no cancel (leaving the page lets the run go on). */
  stop(): void {
    this.#round += 1;
    clearTimeout(this.#timer);
    this.#target = null;
    this.#active = null;
    this.#shown = null;
  }

  /**
   * Submits a query and follows its run.
   *
   * @param environment - The environment to run in.
   * @param sql - The SQL text.
   * @param submitted - Called with the run's id once it exists, before the first poll.
   * @returns A promise that settles once the submit was answered.
   */
  submit(environment: string, sql: string, submitted: (id: string) => void): Promise<void> {
    this.stop();
    this.#show({ ...IDLE, phase: "submitting" });
    const round = this.#round;
    return submitQuery(environment, sql).then(
      this.#current(round, (id: string) => {
        const target = { environment, id };
        this.#target = target;
        submitted(id);
        this.#follow(target);
      }),
      this.#current(round, (error: unknown) => {
        this.#show({ ...IDLE, phase: "refused", error });
      }),
    );
  }

  /**
   * Follows a run an address names.
   *
   * @param environment - The run's environment.
   * @param id - The run's id.
   * @returns Whether it was a run not followed already.
   */
  open(environment: string, id: string): boolean {
    if (this.#target?.environment === environment && this.#target.id === id) {
      return false;
    }
    this.stop();
    const target = { environment, id };
    this.#target = target;
    this.#show({ ...IDLE, opened: true });
    this.#follow(target);
    return true;
  }

  /** Follows the run again; nothing before a run. */
  retry(): void {
    if (this.#target !== null) {
      this.#follow(this.#target);
    }
  }

  /**
   * Cancels the active run, once.
   *
   * @returns Whether a run was active.
   */
  cancel(): boolean {
    const active = this.#active;
    if (active === null) {
      return false;
    }
    if (!this.#cancelSent) {
      this.#cancelSent = true;
      this.#show((view) => ({ ...view, cancelling: true, cancelRefusal: null }));
      cancelQuery(active.environment, active.id).catch(
        this.#current(this.#round, (error: unknown) => {
          // 409: the run ended before the cancel arrived; the next poll says how.
          if (error instanceof ApiError && error.status === 409) {
            return;
          }
          // Refused or not sent: Cancel can be pressed again, and the alert says why.
          this.#cancelSent = false;
          this.#show((view) => ({ ...view, cancelling: false, cancelRefusal: error }));
        }),
      );
    }
    return true;
  }

  /**
   * Reads the page after the rows shown and appends it.
   *
   * @returns A promise that settles once the page was read; it rejects with the refusal.
   */
  more(): Promise<void> {
    const shown = this.#shown;
    const offset = shown?.page.next_offset ?? null;
    if (shown === null || offset === null) {
      return Promise.resolve();
    }
    const { target, page } = shown;
    return getResultPageAt(target.environment, target.id, offset, PAGE_ROWS).then(
      this.#current(this.#round, (next: ResultPageOut) => {
        this.#showPage(target, appendPage(page, next));
      }),
    );
  }

  /**
   * Wraps a step so that it runs only while its round is the current one.
   *
   * @template T - What the step takes.
   * @param round - The step's round.
   * @param step - The step.
   * @returns The wrapped step.
   */
  #current<T>(round: number, step: (value: T) => void): (value: T) => void {
    return (value) => {
      if (round === this.#round) {
        step(value);
      }
    };
  }

  /**
   * Starts a new round that polls a run.
   *
   * @param target - The run.
   */
  #follow(target: Target): void {
    this.#round += 1;
    clearTimeout(this.#timer);
    this.#active = target;
    this.#cancelSent = false;
    this.#show((view) => ({
      ...view,
      phase: "waiting",
      error: null,
      cancelling: false,
      cancelRefusal: null,
    }));
    this.#poll(this.#round, target, 0, 0);
  }

  /**
   * Reads the run's state, and waits for the next poll, reads its first page, or ends.
   *
   * @param round - The round.
   * @param target - The run.
   * @param waited - How long it has been followed.
   * @param failures - How many polls in a row failed before this one.
   */
  #poll(round: number, target: Target, waited: number, failures: number): void {
    getQueryRun(target.environment, target.id).then(
      this.#current(round, (run: QueryRunOut) => {
        if (ACTIVE_STATUSES.includes(run.status)) {
          this.#show((view) => ({ ...view, phase: "waiting", run }));
          const wait = waited < POLL_FAST_FOR_MS ? POLL_FAST_MS : POLL_SLOW_MS;
          this.#later(wait, () => {
            this.#poll(round, target, waited + wait, 0);
          });
          return;
        }
        this.#active = null;
        if (run.status === "SUCCEEDED") {
          this.#show((view) => ({
            ...view,
            phase: "loading_page",
            run,
            cancelling: false,
            cancelRefusal: null,
          }));
          this.#readFirst(round, target);
        } else {
          this.#show((view) => ({
            ...view,
            phase: "done",
            run,
            page: null,
            cancelling: false,
            cancelRefusal: null,
          }));
        }
      }),
      this.#current(round, (error: unknown) => {
        if (failures + 1 < POLL_FAILURES) {
          this.#later(POLL_RETRY_MS, () => {
            this.#poll(round, target, waited + POLL_RETRY_MS, failures + 1);
          });
          return;
        }
        this.#active = null;
        this.#show((view) => ({
          ...view,
          phase: "failed",
          error,
          cancelling: false,
          cancelRefusal: null,
        }));
      }),
    );
  }

  /**
   * Reads the first page of a run that succeeded; a 409 means its result is no longer kept.
   *
   * @param round - The round.
   * @param target - The run.
   */
  #readFirst(round: number, target: Target): void {
    getResultPageAt(target.environment, target.id, 0, PAGE_ROWS).then(
      this.#current(round, (page: ResultPageOut) => {
        this.#showPage(target, page);
      }),
      this.#current(round, (error: unknown) => {
        if (error instanceof ApiError && error.status === 409) {
          this.#show((view) => ({ ...view, phase: "done", page: null }));
        } else {
          this.#show((view) => ({ ...view, phase: "failed", error }));
        }
      }),
    );
  }

  /**
   * Shows the rows of a run.
   *
   * @param target - The run.
   * @param page - The rows.
   */
  #showPage(target: Target, page: ResultPageOut): void {
    this.#shown = { target, page };
    this.#show((view) => ({ ...view, phase: "done", page }));
  }

  /**
   * Runs a step after a while.
   *
   * @param ms - How long to wait.
   * @param step - The step.
   */
  #later(ms: number, step: () => void): void {
    this.#timer = setTimeout(step, ms);
  }
}

/**
 * The page's one run: submit, follow, cancel and read it.
 *
 * @remarks
 * Unlike the Preview tab, it never cancels a run when the page is left (unmount, `pagehide` or
 * a navigation): the run goes on, Recent lists it, and its address shows it again. Every run
 * submitted or opened is recorded for Home's Recently opened.
 * @returns The run's state and what can be done with it.
 */
export function useQueryRun(): QueryRun {
  const navigate = useNavigate();
  const me = useMe();
  const [view, setView] = useState<View>(IDLE);
  const [follower] = useState(() => new Follower(setView));

  useEffect(
    () => () => {
      follower.stop();
    },
    [follower],
  );

  const record = useCallback(
    (environment: string, id: string) => {
      recordOpened(me.name, "query", environment, id).catch(ignore);
    },
    [me.name],
  );
  const submit = useCallback(
    (environment: string, sql: string, state?: object) =>
      follower.submit(environment, sql, (id) => {
        record(environment, id);
        void navigate(runPath(environment, id), { state: { ...state, keepFocus: true } });
      }),
    [follower, navigate, record],
  );
  const open = useCallback(
    (environment: string, id: string) => {
      if (follower.open(environment, id)) {
        record(environment, id);
      }
    },
    [follower, record],
  );
  const cancel = useCallback(() => follower.cancel(), [follower]);
  const retry = useCallback(() => {
    follower.retry();
  }, [follower]);
  const more = useCallback(() => follower.more(), [follower]);
  const reset = useCallback(() => {
    follower.stop();
    setView(IDLE);
  }, [follower]);
  return { ...view, submit, cancel, open, retry, more, reset };
}
