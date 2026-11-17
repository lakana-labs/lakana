/**
 * The Connections list: the connections of the environments the person may read, each with its
 * kind, where it points, its environment, its last test and the assets that use it.
 *
 * @packageDocumentation
 */
import "./connections.css";

import { type JSX, useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  type ConnectionEnvironmentOut,
  type ConnectionOut,
  type ConnectionQueuedTestOut,
  listConnectionEnvironments,
  listConnections,
} from "../api";
import { Unknown } from "../catalog/cells";
import { ListSkeleton } from "../catalog/ListSkeleton";
import { ReadFailed } from "../catalog/ReadFailed";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { EnvironmentPicker } from "../queries/EnvironmentPicker";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { useMe } from "../shell/me";
import { Page } from "../shell/Page";
import { shellQuote } from "../shell/shellQuote";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { whereText } from "./kinds";
import { connectionPath, NEW_CONNECTION_PATH } from "./paths";
import { readFilter, writeFilter } from "./remember";
import { TestAll } from "./TestAll";
import { TestButton } from "./TestButton";
import { TestStatus } from "./TestStatus";
import { useNow } from "./useNow";
import { FollowedAnswers, testKey, useOpenTests } from "./useOpenTests";
import "./connectionStrings";

/** Properties of {@link ConnectionRows} and {@link ConnectionTable}. */
interface ConnectionRowsProps {
  /** The environments whose connections the person may read. */
  readonly environments: readonly ConnectionEnvironmentOut[];
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
 * @returns The table, or the words for a filter with no connection.
 */
function ConnectionTable({
  environments,
  first,
  filter,
}: Omit<ConnectionRowsProps, "onFilter">): JSX.Element {
  const { t } = useTranslation();
  const now = useNow();
  const pages = useCursorPages(filter ?? "", (cursor) => listConnections(filter, cursor));
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
        rowCount={rows.length}
        empty=""
        region={t("connections.list.region")}
        regionRef={region.target}
        rowsOpen
        scroll="connections-scroll"
        head={
          <tr>
            <th scope="col">{t("connections.list.name")}</th>
            <th scope="col">{t("connections.list.kind")}</th>
            <th scope="col" className="narrow-hide">
              {t("connections.list.where")}
            </th>
            <th scope="col">{t("connections.list.environment")}</th>
            <th scope="col">{t("connections.list.status")}</th>
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
        {rows.map((row) => {
          const key = testKey(row.environment, row.name);
          const where = whereText(row.kind, row.address);
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
              <td>{t(`connections.kinds.${row.kind}.name`)}</td>
              <td className="narrow-hide">
                <VisibleText text={where} breakable />
              </td>
              <td>
                <EnvironmentLabel environment={row.environment} />
              </td>
              <td>
                <TestStatus connection={row} now={now} queuedSince={follow.queuedSince.get(key)} />
              </td>
              <td className="number narrow-hide">
                {used === 0 ? <Unknown words={usedText} /> : usedText}
              </td>
              <td>
                {row.actions.includes("test") && (
                  <TestButton connection={row} onStarted={replace} />
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
  return (
    <>
      <EnvironmentPicker
        environments={environments.map(({ name }) => ({ name, protected: false }))}
        value={filter}
        label={t("connections.list.environment")}
        all={t("connections.list.all")}
        onChange={onFilter}
      />
      <ConnectionTable
        key={filter ?? ""}
        environments={environments}
        first={first}
        filter={filter}
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
  const load = useCallback(() => listConnectionEnvironments(), []);
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
      {body}
    </Page>
  );
}
