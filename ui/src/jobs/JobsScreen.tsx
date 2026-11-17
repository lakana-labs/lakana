/**
 * The Jobs area's screen, loaded on demand: a job, a run and a task of a run.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense } from "react";
import { useTranslation } from "react-i18next";
import { Route, Routes } from "react-router";

import { SHIPPED_AREAS } from "../shell/areas";
import { AreaPage } from "../shell/AreaPage";
import { NotFound } from "../shell/NotFound";
import "./jobs.css";
import "./jobStrings";
import { Reading } from "./Reading";

// Each page is its own chunk, read only when its address opens.
const JobPage = lazy(() => import("./JobPage").then((m) => ({ default: m.JobPage })));
const RunPage = lazy(() => import("./RunPage").then((m) => ({ default: m.RunPage })));
const TaskPage = lazy(() => import("./TaskPage").then((m) => ({ default: m.TaskPage })));

/**
 * The area's own page, until the list of jobs and runs arrives in a later change.
 *
 * @returns The page: the area's purpose and its command line.
 */
function JobsIndex(): JSX.Element {
  return (
    <>
      {SHIPPED_AREAS.filter((area) => area.id === "jobs").map((area) => (
        <AreaPage key={area.id} area={area} />
      ))}
    </>
  );
}

/**
 * The Jobs screen: the area's page at `/jobs`, a job at `/jobs/{environment}/job/{repository}/{job}`,
 * a run at `/jobs/{environment}/runs/{id}`, a task at `/jobs/{environment}/runs/{id}/tasks/{asset}`,
 * and the not-found state for every other address. The segment counts keep the area's own tabs
 * (`/jobs/runs`) apart from the detail pages. While the job's, the run's or the task's chunk loads, its
 * skeleton says what is loading after a second.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  const { t } = useTranslation();
  return (
    <Suspense fallback={<></>}>
      <Routes>
        <Route index element={<JobsIndex />} />
        <Route
          path=":env/job/:repository/:job"
          element={
            <Suspense fallback={<Reading label={t("jobs.loadingJob")} />}>
              <JobPage />
            </Suspense>
          }
        />
        <Route
          path=":env/runs/:id"
          element={
            <Suspense fallback={<Reading label={t("jobs.loadingRun")} />}>
              <RunPage />
            </Suspense>
          }
        />
        <Route
          path=":env/runs/:id/tasks/:asset"
          element={
            <Suspense fallback={<Reading label={t("jobs.loadingTask")} />}>
              <TaskPage />
            </Suspense>
          }
        />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  );
}
