/**
 * Runs a table's preview while its tab is open: asks whether the person may run queries, queues
 * the preview, polls it, reads its page, and cancels it when the tab is left.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ACTIVE_STATUSES,
  ApiError,
  cancelQuery,
  getQueryStatus,
  getResultPage,
  myPermissions,
  submitPreview,
} from "../../api";
import type { QueryRunStatus, ResultPageOut, TableOut, TableRef } from "../../api";
import { useMe } from "../../shell/me";
import { problemText } from "../../sign-in/messages";
import type { StatusKind } from "../../status/status";

/** Where a preview is: asking, refused before asking, waiting for its run, or ended. */
type PreviewPhase = "checking" | "not_allowed" | "artifact" | "waiting" | "done" | "failed";

/** What the Preview tab shows. */
export interface PreviewState {
  /** Where the preview is. */
  readonly phase: PreviewPhase;
  /** The run's latest state; null before the first poll. */
  readonly status: QueryRunStatus | null;
  /** The page, once read. */
  readonly page: ResultPageOut | null;
  /** The cause and next step of a failed preview; else null. */
  readonly error: string | null;
}

/** The glyph of each state of a run. */
export const statusGlyph: Record<QueryRunStatus, StatusKind> = {
  QUEUED: "queued",
  RUNNING: "running",
  SUCCEEDED: "succeeded",
  FAILED: "failed",
  CANCELLED: "cancelled",
};

/** How often to poll at first, and for how long. */
const FAST_POLL_MS = 500;
const FAST_FOR_MS = 5_000;
/** How often to poll after that. */
const SLOW_POLL_MS = 2_000;
/** How long to wait before reading a page again after the server answered 503 (`Retry-After: 1`). */
const BUSY_WAIT_MS = 1_000;
/** How often to read a busy page again before saying so. */
const BUSY_RETRIES = 5;

const CHECKING: PreviewState = { phase: "checking", status: null, page: null, error: null };
const ARTIFACT: PreviewState = { ...CHECKING, phase: "artifact" };

/**
 * Ignores a failed cancel: the run has ended already, or the server will end it at its timeout,
 * and the person has left the tab.
 *
 * @returns Nothing.
 */
function ignore(): undefined {
  return undefined;
}

/**
 * Runs the preview of a table while the calling component is mounted, once per `refresh`.
 *
 * @remarks
 * The run's id and the `alive` flag live in the effect: its cleanup clears the one timer and
 * cancels a run still `QUEUED` or `RUNNING`, and a submission that answers after the cleanup
 * cancels its own run. StrictMode's mount, unmount and mount so leaves no run behind.
 * @param ref - The table.
 * @param kind - What it is; an artifact has no preview, and nothing is asked for one.
 * @returns The state and `refresh`, which cancels a waiting run and runs the preview again.
 */
export function usePreview(
  ref: TableRef,
  kind: TableOut["kind"],
): PreviewState & { readonly refresh: () => void } {
  const { t } = useTranslation();
  const me = useMe();
  const [state, setState] = useState<PreviewState>(kind === "ARTIFACT" ? ARTIFACT : CHECKING);
  const [round, setRound] = useState(0);
  const { environment, namespace, name } = ref;

  useEffect(() => {
    if (kind === "ARTIFACT") {
      return undefined;
    }
    let alive = true;
    let runId: string | null = null;
    let active = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const show = (next: PreviewState): void => {
      if (alive) {
        setState(next);
      }
    };
    const fail = (error: string): void => {
      show({ phase: "failed", status: null, page: null, error });
    };
    const refused = (error: unknown): void => {
      fail(problemText(t, error));
    };
    const later = (ms: number, step: () => void): void => {
      timer = setTimeout(step, ms);
    };

    const read = (id: string, retries: number): void => {
      getResultPage(environment, id).then(
        (page) => {
          show({ phase: "done", status: "SUCCEEDED", page, error: null });
        },
        (error: unknown) => {
          if (!alive) {
            return;
          }
          if (!(error instanceof ApiError) || error.status !== 503) {
            refused(error);
          } else if (retries < BUSY_RETRIES) {
            later(BUSY_WAIT_MS, () => {
              read(id, retries + 1);
            });
          } else {
            fail(t("table.preview.busy"));
          }
        },
      );
    };

    const poll = (id: string, waited: number): void => {
      getQueryStatus(environment, id).then((answer) => {
        if (!alive) {
          return;
        }
        if (ACTIVE_STATUSES.includes(answer.status)) {
          show({ phase: "waiting", status: answer.status, page: null, error: null });
          const wait = waited < FAST_FOR_MS ? FAST_POLL_MS : SLOW_POLL_MS;
          later(wait, () => {
            poll(id, waited + wait);
          });
          return;
        }
        active = false;
        if (answer.status === "SUCCEEDED") {
          show({ phase: "waiting", status: answer.status, page: null, error: null });
          read(id, 0);
        } else {
          show({
            phase: "failed",
            status: answer.status,
            page: null,
            error: answer.error_message ?? t("table.preview.failed"),
          });
        }
      }, refused);
    };

    myPermissions(me.name, { environment })
      .then((answer) => {
        if (!alive) {
          return undefined;
        }
        const mayQuery = answer.permissions.some(
          (permission) => permission.action === "SUBMIT_QUERY" && permission.allowed,
        );
        if (!mayQuery) {
          show({ ...CHECKING, phase: "not_allowed" });
          return undefined;
        }
        show({ ...CHECKING, phase: "waiting" });
        return submitPreview({ environment, namespace, name }).then((id) => {
          if (!alive) {
            // The tab was left while the preview was being queued: no run is left behind.
            cancelQuery(environment, id).catch(ignore);
            return;
          }
          runId = id;
          active = true;
          poll(id, 0);
        });
      })
      .catch(refused);

    return () => {
      alive = false;
      clearTimeout(timer);
      if (runId !== null && active) {
        cancelQuery(environment, runId).catch(ignore);
      }
    };
  }, [environment, namespace, name, kind, me.name, round, t]);

  const refresh = useCallback(() => {
    setState(CHECKING);
    setRound((previous) => previous + 1);
  }, []);
  return { ...state, refresh };
}
