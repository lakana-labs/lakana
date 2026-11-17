/**
 * The Queries screen's library: Saved and Recent, under one filter by environment.
 *
 * @packageDocumentation
 */
import { type JSX, useId } from "react";
import { useTranslation } from "react-i18next";

import type { QueryEnvironmentOut } from "../api/queries";
import { EnvironmentPicker } from "./EnvironmentPicker";
import { RecentList } from "./RecentList";
import { writeFilter } from "./remember";
import { SavedList } from "./SavedList";
import "./queryStrings";

/** Properties of {@link Library}. */
export interface LibraryProps {
  /** The person's principal name, whose recent queries are listed. */
  readonly me: string;
  /** The environments where the person may run a query: the filter's choices. */
  readonly environments: readonly QueryEnvironmentOut[];
  /** The environment both lists are filtered by, or null for all of them. */
  readonly filter: string | null;
  /** Called with the filter chosen; it is remembered in this browser first. */
  readonly onFilter: (environment: string | null) => void;
  /** A number that changes when both lists must be read again, as after a save or a delete. */
  readonly reloadKey: number;
}

/**
 * Renders the filter, then Saved and Recent. A remembered filter that is no longer listed (the
 * environment was deleted, or the right revoked) lists all environments and names none.
 *
 * @param props - The person, the environments, the filter and the reload key.
 * @param props.me - The person's principal name.
 * @param props.environments - The filter's choices.
 * @param props.filter - The environment, or null for all.
 * @param props.onFilter - Called with the filter chosen.
 * @param props.reloadKey - Changes when the lists must be read again.
 * @returns The library.
 */
export function Library({
  me,
  environments,
  filter,
  onFilter,
  reloadKey,
}: LibraryProps): JSX.Element {
  const { t } = useTranslation();
  const saved = useId();
  const recent = useId();
  const shown = environments.some((environment) => environment.name === filter) ? filter : null;
  return (
    <div className="queries-library-lists">
      <EnvironmentPicker
        environments={environments}
        value={shown}
        label={t("queries.library.filter")}
        all={t("queries.library.all")}
        onChange={(chosen: string | null) => {
          writeFilter(chosen);
          onFilter(chosen);
        }}
      />
      <section aria-labelledby={saved}>
        <h2 id={saved}>{t("queries.library.saved")}</h2>
        <SavedList me={me} filter={shown} reloadKey={reloadKey} />
      </section>
      <section aria-labelledby={recent}>
        <h2 id={recent}>{t("queries.library.recent")}</h2>
        <RecentList me={me} filter={shown} reloadKey={reloadKey} />
      </section>
    </div>
  );
}
