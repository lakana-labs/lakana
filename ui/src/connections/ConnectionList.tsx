/**
 * The Connections list: the connections of the environments the person may read, each with its
 * kind, where it points, its environment, its last test and the assets that use it.
 *
 * @packageDocumentation
 */
import "./connections.css";

import { type JSX, useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  type ConnectionEnvironmentOut,
  type ConnectionLastTestOut,
  type ConnectionOut,
  listConnectionEnvironments,
  listConnections,
  requestConnectionTest,
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
import { PendingButton } from "../shell/PendingButton";
import { shellQuote } from "../shell/shellQuote";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { problemText } from "../sign-in/messages";
import { whereText } from "./kinds";
import { connectionPath, NEW_CONNECTION_PATH } from "./paths";
import { readFilter, writeFilter } from "./remember";
import { TestStatus } from "./TestStatus";
import { useNow } from "./useNow";
import "./connectionStrings";

/** Properties of {@link ConnectionRows}. */
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
 * Renders the filter and the table, or the words for a filter with no connection.
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
  const now = useNow();
  const pages = useCursorPages(filter ?? "", (cursor) => listConnections(filter, cursor));
  // A test requested here, shown until the list is read again; and the refusal of one.
  const [tests, setTests] = useState<Readonly<Record<string, ConnectionLastTestOut>>>({});
  const [refusals, setRefusals] = useState<Readonly<Record<string, string>>>({});
  const [working, setWorking] = useState<ReadonlySet<string>>(new Set());

  const rows = (pages.items ?? []).map((row): ConnectionOut => {
    const test = tests[`${row.environment}/${row.name}`];
    return test === undefined ? row : { ...row, last_test: test };
  });

  const test = async (row: ConnectionOut): Promise<void> => {
    const id = `${row.environment}/${row.name}`;
    setWorking((previous) => new Set(previous).add(id));
    setRefusals((previous) =>
      Object.fromEntries(Object.entries(previous).filter(([other]) => other !== id)),
    );
    try {
      const requested = await requestConnectionTest(row.environment, row.name);
      setTests((previous) => ({ ...previous, [id]: requested }));
    } catch (failure: unknown) {
      setRefusals((previous) => ({ ...previous, [id]: problemText(t, failure) }));
    } finally {
      setWorking((previous) => new Set([...previous].filter((other) => other !== id)));
    }
  };

  const none = pages.items?.length === 0 && !pages.busy && !pages.hasMore;
  const commandEnvironment = filter ?? first;
  return (
    <>
      <EnvironmentPicker
        environments={environments.map(({ name }) => ({ name, protected: false }))}
        value={filter}
        label={t("connections.list.environment")}
        all={t("connections.list.all")}
        onChange={onFilter}
      />
      {none ? (
        <div className="empty">
          <p>
            {filter === null
              ? t("connections.list.none")
              : t("connections.list.noneIn", { environment: markedText(filter) })}
          </p>
          <p>{t("connections.list.noneCommand")}</p>
          <CopyLine
            text={`lakana connection create NAME --env ${shellQuote(commandEnvironment)} --kind directory --root ROOT --path PATH`}
            copyLabel={t("connections.list.copy")}
          />
        </div>
      ) : (
        <ViewList
          pages={pages}
          rowCount={rows.length}
          empty=""
          region={t("connections.list.region")}
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
            const id = `${row.environment}/${row.name}`;
            const where = whereText(row.kind, row.address);
            const used = row.declared_by.length;
            const usedText =
              used === 0
                ? t("connections.list.usedNone")
                : t("connections.list.used", { count: used });
            const open = row.last_test?.state === "queued" || row.last_test?.state === "running";
            return (
              <tr key={id}>
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
                  {refusals[id] !== undefined && (
                    <span className="step-error second" role="alert">
                      {refusals[id]}
                    </span>
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
                  <TestStatus connection={row} now={now} />
                </td>
                <td className="number narrow-hide">
                  {used === 0 ? <Unknown words={usedText} /> : usedText}
                </td>
                <td>
                  {row.actions.includes("test") && (
                    <PendingButton
                      className="button"
                      pending={working.has(id)}
                      isDisabled={open}
                      aria-label={t("connections.list.testNamed", { name: markedText(row.name) })}
                      onPress={() => {
                        void test(row);
                      }}
                    >
                      {open ? t("connections.list.testing") : t("connections.list.test")}
                    </PendingButton>
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
      )}
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
