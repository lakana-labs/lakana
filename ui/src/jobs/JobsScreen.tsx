/**
 * The Jobs area's screen, loaded on demand: its three tabs (the list of jobs and runs, every run,
 * an environment's deploys), a job, a run and a task of a run.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, type ReactNode, Suspense } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, Route, Routes, useSearchParams } from "react-router";

import { NotFound } from "../shell/NotFound";
import { Page } from "../shell/Page";
import "./jobs.css";
import "./jobStrings";
import { deploysPath, jobsListPath, runsTabPath } from "./paths";
import { Reading } from "./Reading";

// Each page is its own chunk, read only when its address opens.
const JobsList = lazy(() => import("./JobsList").then((m) => ({ default: m.JobsList })));
const RunsTab = lazy(() => import("./RunsTab").then((m) => ({ default: m.RunsTab })));
const DeploysTab = lazy(() => import("./DeploysTab").then((m) => ({ default: m.DeploysTab })));
const JobPage = lazy(() => import("./JobPage").then((m) => ({ default: m.JobPage })));
const RunPage = lazy(() => import("./RunPage").then((m) => ({ default: m.RunPage })));
const TaskPage = lazy(() => import("./TaskPage").then((m) => ({ default: m.TaskPage })));

/** Properties of {@link TabPage}. */
interface TabPageProps {
  /** The page's heading and title, such as "Runs". */
  readonly title: string;
  /** What the skeleton says while the tab's chunk loads. */
  readonly loading: string;
  /** The tab's content, loaded on demand. */
  readonly children: ReactNode;
}

/**
 * A tab of the area, at `/jobs`, `/jobs/runs` or `/jobs/deploys`: its heading at once, so the
 * focus the shell puts on it after a move survives the tab's own chunk, the row of tabs Jobs,
 * Runs and Deploys (each keeping the address's environment), and the tab under it. Only these
 * three pages show the row; a detail page has its breadcrumb instead. The three are wider than a
 * detail page, so that their tables fit beside the sidebar from a 1280 px window on.
 *
 * @param props - The title, the skeleton's words and the content.
 * @param props.title - The page's heading.
 * @param props.loading - What the skeleton says.
 * @param props.children - The tab's content.
 * @returns The page.
 */
function TabPage({ title, loading, children }: TabPageProps): JSX.Element {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const environment = params.get("environment");
  return (
    <div className="jobs-tab-page">
      <Page title={title}>
        {/* The row of `SectionTabs`, written out here: shared with a table's page, that component
          would be a chunk of its own, which the first load would have to name. */}
        <nav className="tabs" aria-label={t("jobs.tabs.label")}>
          <NavLink className="tab" to={jobsListPath(environment)} end>
            {t("jobs.tabs.jobs")}
          </NavLink>
          <NavLink className="tab" to={runsTabPath({ environment })} end>
            {t("jobs.tabs.runs")}
          </NavLink>
          <NavLink className="tab" to={deploysPath(environment)} end>
            {t("jobs.tabs.deploys")}
          </NavLink>
        </nav>
        <Suspense fallback={<Reading label={loading} />}>{children}</Suspense>
      </Page>
    </div>
  );
}

/**
 * The Jobs screen: the list at `/jobs`, every run at `/jobs/runs`, deploys at `/jobs/deploys`, a
 * job at `/jobs/{environment}/job/{repository}/{job}`,
 * a run at `/jobs/{environment}/runs/{id}`, a task at `/jobs/{environment}/runs/{id}/tasks/{asset}`,
 * and the not-found state for every other address. The segment counts keep the area's own tabs
 * (`/jobs/runs`) apart from the detail pages. While the list's, the job's, the run's or the task's
 * chunk loads, its skeleton says what is loading after a second.
 *
 * @returns The place the address names.
 */
export function AreaScreen(): JSX.Element {
  const { t } = useTranslation();
  return (
    <Suspense fallback={<></>}>
      <Routes>
        <Route
          index
          element={
            <TabPage title={t("areas.jobs.name")} loading={t("jobs.list.loading")}>
              <JobsList />
            </TabPage>
          }
        />
        <Route
          path="runs"
          element={
            <TabPage title={t("jobs.tabs.runs")} loading={t("jobs.tabs.loadingRuns")}>
              <RunsTab />
            </TabPage>
          }
        />
        <Route
          path="deploys"
          element={
            <TabPage title={t("jobs.tabs.deploys")} loading={t("jobs.tabs.loadingDeploys")}>
              <DeploysTab />
            </TabPage>
          }
        />
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
