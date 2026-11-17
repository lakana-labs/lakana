/**
 * A run's page: the run with its facts, controls and tasks, under the breadcrumb through its job.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useRef } from "react";
import { useParams } from "react-router";

import { recordOpened } from "../api";
import type { RunOut } from "../api/jobs";
import { useMe } from "../shell/me";
import "./jobStrings";
import { RunDetail } from "./RunDetail";

/**
 * Renders the run the address names and records it, once read, as opened for Home.
 *
 * @returns The page.
 */
export function RunPage(): JSX.Element {
  const { env = "", id = "" } = useParams();
  const me = useMe();
  const recorded = useRef(new Set<string>());
  const record = useCallback(
    (run: RunOut) => {
      if (!recorded.current.has(run.id)) {
        recorded.current.add(run.id);
        // Recently opened is a convenience: a failed record is not the person's concern.
        recordOpened(me.name, "run", env, run.id).catch(() => undefined);
      }
    },
    [me.name, env],
  );
  return <RunDetail environment={env} runId={id} level={1} onRunChanged={record} />;
}
