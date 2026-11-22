/**
 * The editor bar of the Queries screen: the query's name, New query, Save…, Delete…, Format, the
 * environment and Run, which becomes Cancel while the run is active.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode, Ref } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { QueryEnvironmentOut } from "../api/queries";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { PendingButton } from "../shell/PendingButton";
import { shortcutLabel } from "../shell/shortcut";
import { VisibleText } from "../shell/VisibleText";
import { NEW_QUERY_KEYS } from "./newQueryKeys";
import type { QueryRun } from "./useQueryRun";
import "./queryStrings";

/** Properties of {@link QueryToolbar}. */
export interface QueryToolbarProps {
  /** The Queries ▾ button of a narrow screen, first in the bar; null beside the library. */
  readonly fold: ReactNode;
  /** The query's name; empty while a saved query's address is read. */
  readonly name: string;
  /** Whether nothing may be saved, formatted or run (a saved query not read, or not readable). */
  readonly readOnly: boolean;
  /** Whether the opened saved query may be deleted, which shows Delete…. */
  readonly deletable: boolean;
  /** The environments where the person may run a query. */
  readonly environments: readonly QueryEnvironmentOut[];
  /** The chosen environment. */
  readonly environment: string;
  /** Whether a run may be sent to the chosen environment. */
  readonly allowed: boolean;
  /** The run, for Run and Cancel. */
  readonly query: QueryRun;
  /** Run's button, which Try again focuses. */
  readonly runButton: Ref<HTMLButtonElement>;
  /** Starts a new query, asking first when the editor holds changes that are not saved. */
  readonly onNew: () => void;
  /** Opens Save…. */
  readonly onSave: () => void;
  /** Opens Delete…. */
  readonly onDelete: () => void;
  /** Formats the SQL. */
  readonly onFormat: () => void;
  /** Called with the environment chosen. */
  readonly onEnvironment: (chosen: string) => void;
  /** Runs the editor's SQL. */
  readonly onRun: () => void;
}

/**
 * Renders the bar. Run is one button that becomes Cancel while the run is active, so the focus
 * stays on it; its keys stand beside it, not in it.
 *
 * @param props - What the bar shows and what its controls do.
 * @param props.fold - The Queries ▾ button, or null.
 * @param props.name - The query's name.
 * @param props.readOnly - Whether nothing may be saved, formatted or run.
 * @param props.deletable - Whether Delete… shows.
 * @param props.environments - The environments to choose from.
 * @param props.environment - The chosen environment.
 * @param props.allowed - Whether a run may be sent there.
 * @param props.query - The run.
 * @param props.runButton - Run's button.
 * @param props.onNew - Starts a new query.
 * @param props.onSave - Opens Save….
 * @param props.onDelete - Opens Delete….
 * @param props.onFormat - Formats the SQL.
 * @param props.onEnvironment - Called with the environment chosen.
 * @param props.onRun - Runs the SQL.
 * @returns The bar.
 */
export function QueryToolbar({
  fold,
  name,
  readOnly,
  deletable,
  environments,
  environment,
  allowed,
  query,
  runButton,
  onNew,
  onSave,
  onDelete,
  onFormat,
  onEnvironment,
  onRun,
}: QueryToolbarProps): JSX.Element {
  const { t } = useTranslation();
  const active = query.phase === "waiting";
  return (
    <div className="queries-toolbar">
      {fold}
      <p className="queries-name">
        <VisibleText text={name} />
      </p>
      {/* A native button: React Aria's Button drops aria-keyshortcuts. */}
      <button type="button" className="button" aria-keyshortcuts={NEW_QUERY_KEYS} onClick={onNew}>
        {t("queries.new.open")}
      </button>
      <Button className="button" isDisabled={readOnly} onPress={onSave}>
        {t("queries.save.open")}
      </Button>
      {deletable && (
        <Button className="button" onPress={onDelete}>
          {t("queries.delete.open")}
        </Button>
      )}
      <Button className="button" isDisabled={readOnly} onPress={onFormat}>
        {t("queries.format")}
      </Button>
      <EnvironmentPicker environments={environments} value={environment} onChange={onEnvironment} />
      {/* One button that is Run, or Cancel while the run is active, so the focus stays on it. */}
      <PendingButton
        ref={runButton}
        className={active ? "button queries-run" : "button button-primary queries-run"}
        pending={query.phase === "submitting" || query.cancelling}
        isDisabled={readOnly || (!active && !allowed)}
        onPress={active ? query.cancel : onRun}
      >
        {active ? t(query.cancelling ? "queries.cancelling" : "queries.cancel") : t("queries.run")}
      </PendingButton>
      {/* Beside Run, not in it: text inside a button belongs in its name (WCAG 2.5.3). */}
      {!active && (
        <kbd className="queries-run-keys" aria-hidden="true">
          {t(`queries.runKeys.${shortcutLabel(navigator)}`)}
        </kbd>
      )}
    </div>
  );
}
