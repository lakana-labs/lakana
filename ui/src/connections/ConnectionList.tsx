/**
 * The Connections list: the connections of the environments the person may read, each with its
 * kind, where it points, its environment, its last test and the assets that use it.
 *
 * @packageDocumentation
 */
import "./connections.css";

import { type JSX, useCallback, useEffect, useMemo, useState, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate } from "react-router";

import { type ConnectionOut, type ConnectionQueuedTestOut, listConnections } from "../api";
import { Unknown } from "../catalog/cells";
import { ListSkeleton } from "../catalog/ListSkeleton";
import { ReadFailed } from "../catalog/ReadFailed";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { countWords, ListToolbar, type ListState, useListState } from "../shell/ListToolbar";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import type { ListSortSpec } from "../shell/listSort";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { shellQuote } from "../shell/shellQuote";
import { SortHeader } from "../shell/SortHeader";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useListSort } from "../shell/useListSort";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { ConnectionNote, listNote } from "./ConnectionNote";
import { type ConnectionEnvironment, readConnectionEnvironments } from "./environments";
import { isKnownKind, whereText } from "./kinds";
import { connectionPath, NEW_CONNECTION_PATH } from "./paths";
import { readFilter, writeFilter } from "./remember";
import { RunFailure } from "./RunFailure";
import { TestAll } from "./TestAll";
import { TestButton } from "./TestButton";
import { TestStatus } from "./TestStatus";
import { useNow } from "./useNow";
import { FollowedAnswers, testKey, useOpenTests } from "./useOpenTests";
import "./connectionStrings";

/**
 * The columns the connections sort by. Their own order, the newest first, is no column; by state,
 * the failed tests come first, then the open ones, then those that passed, never tested last.
 */
const CONNECTION_SORTS: ListSortSpec<"name" | "kind" | "environment" | "status"> = {
  fields: { name: "asc", kind: "asc", environment: "asc", status: "asc" },
  natural: null,
};

/** Properties of {@link ConnectionRows} and {@link ConnectionTable}. */
interface ConnectionRowsProps {
  /** The environments whose connections the person may read, each saying if it is protected. */
  readonly environments: readonly ConnectionEnvironment[];
  /** The name of the first of them, for the command that adds a connection. */
  readonly first: string;
  /** The environment the list is filtered by, or null for all of them. */
  readonly filter: string | null;
  /** Called with the environment chosen, or null for all of them. */
  readonly onFilter: (environment: string | null) => void;
}

/**
 * Renders the table of one filter, Test all below it, and follows every open test on screen.
 * What a read of a followed connection answers takes the place of its row; nothing of it
 * outlives the filter, since a new filter mounts a new table.
 *
 * @param props - The environments and the filter.
 * @param props.environments - The environments the person may read.
 * @param props.first - The first environment's name.
 * @param props.filter - The environment filtered by, or null.
 * @param props.onState - Told the rows shown and the state of the read, for the toolbar.
 * @param props.round - Changes when Refresh asks for the list again.
 * @returns The table, or the words for a filter with no connection.
 */
function ConnectionTable({
  environments,
  first,
  filter,
  onState,
  round,
}: Omit<ConnectionRowsProps, "onFilter"> & {
  /** Told the rows shown and the state of the read, for the toolbar above the table. */
  readonly onState: (state: ListState) => void;
  /** Changes when Refresh asks for the list again. */
  readonly round: number;
}): JSX.Element {
  const { t } = useTranslation();
  const now = useNow();
  const sorting = useListSort(CONNECTION_SORTS);
  const sort = sorting.choice;
  // A new sort reads the first page again; the rows stay until it arrives.
  const pages = useCursorPages(
    filter ?? "",
    (cursor) => listConnections(filter, cursor, undefined, undefined, sort),
    JSON.stringify(sort),
    // A connection whose last test changed while the list is paged by state is shown once.
    (row) => testKey(row.environment, row.name),
  );
  // Refresh reads the list again in place: the rows and the count stay until the answer is in.
  const { reload } = pages;
  const firstRound = useRef(round);
  useEffect(() => {
    if (round !== firstRound.current) reload();
  }, [round, reload]);
  // The latest of each connection a Test, Test all or read of the follower handed over.
  const [answers, setAnswers] = useState<ReadonlyMap<string, ConnectionOut>>(new Map());
  const replace = useCallback((row: ConnectionOut) => {
    setAnswers((previous) => new Map(previous).set(testKey(row.environment, row.name), row));
  }, []);
  const rows = (pages.items ?? []).map(
    (row) => answers.get(testKey(row.environment, row.name)) ?? row,
  );
  const follow = useOpenTests(rows, replace);
  const region = useFocusAfter<HTMLDivElement>(follow.failed);
  const { stuck, gone, failed } = follow;
  // A connection a read found deleted is no longer shown, rather than as "testing…" for good.
  const shown = rows.filter((row) => !gone.has(testKey(row.environment, row.name)));
  const followed = useMemo(
    () => ({ answers, stuck, gone, failed }),
    [answers, stuck, gone, failed],
  );

  const queued = (environment: string, items: readonly ConnectionQueuedTestOut[]): void => {
    // Every named connection's earlier answer is replaced by the new test, so only this run's
    // outcomes count; one never read here has none until the follower reads it.
    setAnswers((previous) => {
      const next = new Map(previous);
      for (const item of items) {
        const key = testKey(environment, item.connection);
        const row = next.get(key) ?? pages.items?.find(({ name }) => name === item.connection);
        if (row !== undefined) {
          next.set(key, {
            ...row,
            last_test: { state: "queued", outcome: null, finished_at: null, id: item.test_id },
          });
        }
      }
      return next;
    });
    follow.open(items.map(({ connection }) => ({ environment, name: connection })));
  };

  const testable = environments
    .filter(({ name }) => filter === null || name === filter)
    .some(({ actions }) => actions.includes("test"));
  const none = pages.items?.length === 0 && !pages.busy && !pages.hasMore;
  useEffect(() => {
    if (none) onState({ shown: 0, more: false, busy: false });
  }, [none, onState]);
  if (none) {
    return (
      <div className="empty">
        <p>
          {filter === null
            ? t("connections.list.none")
            : t("connections.list.noneIn", { environment: markedText(filter) })}
        </p>
        <p>{t("connections.list.noneCommand")}</p>
        <CopyLine
          text={`lakana connection create NAME --env ${shellQuote(filter ?? first)} --kind directory --root ROOT --path PATH`}
          copyLabel={t("connections.list.copy")}
        />
      </div>
    );
  }
  return (
    <>
      <ViewList
        pages={pages}
        rowCount={shown.length}
        empty=""
        region={t("connections.list.region")}
        regionRef={region.target}
        rowsOpen
        scroll="connections-scroll"
        onState={onState}
        head={
          <tr>
            <SortHeader {...sorting.header("name")}>{t("connections.list.name")}</SortHeader>
            <SortHeader {...sorting.header("kind")}>{t("connections.list.kind")}</SortHeader>
            {/* Where is an address of a kind's own parts (a host, a bucket, a path), and Used
                by a count of other projects' declarations: neither has one order in the list. */}
            <th scope="col" className="narrow-hide">
              {t("connections.list.where")}
            </th>
            <SortHeader {...sorting.header("environment")}>
              {t("connections.list.environment")}
            </SortHeader>
            <SortHeader {...sorting.header("status")}>{t("connections.list.status")}</SortHeader>
            <th scope="col" className="number narrow-hide">
              {t("connections.list.usedBy")}
            </th>
            <th scope="col">
              <span className="visually-hidden">{t("connections.list.test")}</span>
            </th>
            <th scope="col">
              <span className="visually-hidden">{t("list.open")}</span>
            </th>
          </tr>
        }
      >
        {shown.map((row) => {
          const key = testKey(row.environment, row.name);
          const known = isKnownKind(row.kind);
          const where = known ? whereText(row.kind, row.address) : "";
          const used = row.declared_by.length;
          const usedText =
            used === 0
              ? t("connections.list.usedNone")
              : t("connections.list.used", { count: used });
          return (
            <tr key={key}>
              <td>
                <Link className="row-name" to={connectionPath(row.environment, row.name)}>
                  <VisibleText text={row.name} />
                </Link>
                <span className="second narrow-only">
                  {t("connections.list.narrowSecond", {
                    where: markedText(where),
                    used: usedText,
                  })}
                </span>
                {follow.stuck.has(key) && (
                  <span className="second">{t("connections.follow.stuck")}</span>
                )}
              </td>
              <td>
                {known ? t(`connections.kinds.${row.kind}.name`) : <VisibleText text={row.kind} />}
              </td>
              <td className="narrow-hide">
                <VisibleText text={where} breakable />
              </td>
              <td>
                <EnvironmentLabel
                  environment={row.environment}
                  protected={environments.some(
                    (environment) => environment.name === row.environment && environment.protected,
                  )}
                />
              </td>
              <td>
                <TestStatus connection={row} now={now} queuedSince={follow.queuedSince.get(key)} />
                {row.last_run_failure != null && (
                  <span className="run-failure-line">
                    <RunFailure
                      environment={row.environment}
                      failure={row.last_run_failure}
                      now={now}
                    />
                  </span>
                )}
              </td>
              <td className="number narrow-hide">
                {used === 0 ? <Unknown words={usedText} /> : usedText}
              </td>
              <td>
                {row.actions.includes("test") && (
                  <TestButton connection={row} at={now} onStarted={replace} />
                )}
              </td>
              <td>
                <span className="chevron" aria-hidden="true">
                  ›
                </span>
              </td>
            </tr>
          );
        })}
      </ViewList>
      {follow.failed && (
        <ReadFailed
          message={t("connections.follow.failed")}
          onRetry={() => {
            region.arm();
            follow.retry();
          }}
        />
      )}
      {testable &&
        (filter === null ? (
          <p className="connections-test-all">{t("connections.testAll.choose")}</p>
        ) : (
          <FollowedAnswers value={followed}>
            <TestAll
              environment={filter}
              onQueued={(items) => {
                queued(filter, items);
              }}
            />
          </FollowedAnswers>
        ))}
    </>
  );
}

/**
 * Renders the filter and, below it, the table of the environment chosen.
 *
 * @param props - The environments and the filter.
 * @param props.environments - The environments the person may read.
 * @param props.first - The first environment's name.
 * @param props.filter - The environment filtered by, or null.
 * @param props.onFilter - Called with the environment chosen.
 * @returns The filter and the list.
 */
function ConnectionRows({
  environments,
  first,
  filter,
  onFilter,
}: ConnectionRowsProps): JSX.Element {
  const { t } = useTranslation();
  const [list, report] = useListState();
  // Refresh reads the table's list again in place (ConnectionTable's `round`).
  const [round, setRound] = useState(0);
  return (
    <>
      <ListToolbar
        count={countWords(t, "connections.list", list.shown, list.more)}
        filters={
          <EnvironmentPicker
            environments={environments}
            value={filter}
            label={t("connections.list.environment")}
            all={t("connections.list.all")}
            onChange={onFilter}
          />
        }
        onRefresh={() => {
          setRound((previous) => previous + 1);
        }}
        refreshing={list.busy}
      />
      <ConnectionTable
        key={filter ?? ""}
        round={round}
        environments={environments}
        first={first}
        filter={filter}
        onState={report}
      />
    </>
  );
}

/**
 * Renders the Connections list. The environment filter is remembered for the person in this
 * browser; one that is no longer listed lists all environments. The list is read once the
 * environments are known, so a gone environment is never asked for.
 *
 * @returns The page.
 */
export function ConnectionList(): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const location = useLocation();
  const navigate = useNavigate();
  // Held from the first render, then dropped from the address, so a reload or Back does not
  // say it again.
  const [note] = useState(() => listNote(location.state));
  useEffect(() => {
    if (note !== null) {
      void navigate(location.pathname, { replace: true, state: null });
    }
    // Once, for the note this visit arrived with.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const load = useCallback(() => readConnectionEnvironments(), []);
  const resource = useResource(load);
  const [chosen, setChosen] = useState(() => readFilter(me.name));
  const environments = resource.data;
  const filter = environments?.some((environment) => environment.name === chosen) ? chosen : null;
  const canCreate = environments?.some((environment) => environment.actions.includes("create"));

  let body: JSX.Element;
  if (environments === undefined) {
    body =
      resource.error === null ? (
        <ListSkeleton />
      ) : (
        <ReadFailed message={resource.error} onRetry={resource.reload} />
      );
  } else if (environments[0] === undefined) {
    body = (
      <div className="empty">
        <p>{t("connections.list.noEnvironment")}</p>
        <p>{t("connections.list.noEnvironmentCommand")}</p>
        <CopyLine
          text="lakana connection list --env <environment>"
          copyLabel={t("connections.list.copy")}
        />
      </div>
    );
  } else {
    body = (
      <ConnectionRows
        environments={environments}
        first={environments[0].name}
        filter={filter}
        onFilter={(environment) => {
          writeFilter(me.name, environment);
          setChosen(environment);
        }}
      />
    );
  }
  return (
    <Page
      title={t("areas.connections.name")}
      action={
        canCreate === true ? (
          <Link className="button button-primary" to={NEW_CONNECTION_PATH}>
            {t("connections.list.add")}
          </Link>
        ) : undefined
      }
    >
      {note !== null && <ConnectionNote note={note} />}
      {body}
    </Page>
  );
}
