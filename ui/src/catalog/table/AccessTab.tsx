/**
 * The Access tab: what the person may do with the table and why, and for an administrator who
 * has access, the row filters and the column masks. Read-only; its code loads only when the tab
 * is opened.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useCallback } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  type ColumnMaskOut,
  type GrantLevel,
  listGrantsAt,
  listInstanceGrants,
  type PermissionOut,
  type RowFilterOut,
} from "../../api";
import { grantsPath, policiesPath } from "../../access/accessPaths";
import { SubjectName } from "../../access/SubjectName";
import type { CursorPage } from "../../api/manage";
import { listColumnMaskPage, listRowFilterPage, type PolicyFilter } from "../../api/policies";
import { useMe } from "../../shell/me";
import { useFocusAfter } from "../../shell/useFocusAfter";
import { type Resource, useResource } from "../../shell/useResource";
import { VisibleText } from "../../shell/VisibleText";
import { StatusGlyph } from "../../status/StatusGlyph";
import { When } from "../cells";
import { ReadFailed } from "../ReadFailed";
import { allowedBecause, editTextsAnswer, grantListCommand, namespaceLevels } from "./reasons";
import { useTableData } from "./TableContext";

/** The actions the tab answers, besides editing the texts, in the order it shows them. */
const ACTIONS = ["READ_TABLE", "WRITE_TABLE", "DROP_TABLE"] as const;

/** One answer of the "What you may do" list. */
interface Answer {
  /** The action's word. */
  readonly label: string;
  /** Whether the person may. */
  readonly allowed: boolean;
  /** Why, or the next step. */
  readonly words: string;
}

/**
 * Read, Write, Drop and Edit texts, each allowed or not, with the reason or the next step.
 *
 * @param props - The answer of the permissions read.
 * @param props.permissions - The person's permissions on the table.
 * @returns The table of answers.
 */
function Answers({ permissions }: { readonly permissions: readonly PermissionOut[] }): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const { table, mayEdit } = useTableData();
  const find = (action: string): PermissionOut | undefined =>
    permissions.find((one) => one.action === action);
  // The server answers every table action, so each of these is in the answer once.
  const answers: Answer[] = ACTIONS.flatMap((action) =>
    permissions
      .filter((permission) => permission.action === action)
      .map((permission) => ({
        label: t(`table.access.actions.${action}`),
        allowed: permission.allowed,
        words: allowedBecause(permission, t),
      })),
  );
  answers.push({
    label: t("table.access.actions.editTexts"),
    ...editTextsAnswer(table, me, mayEdit, find("DROP_TABLE"), t),
  });
  return (
    <table className="list list-static">
      <thead>
        <tr>
          <th scope="col">{t("table.access.mine.action")}</th>
          <th scope="col">{t("table.access.mine.answer")}</th>
          <th scope="col">{t("table.access.mine.why")}</th>
        </tr>
      </thead>
      <tbody>
        {answers.map((answer) => (
          <tr key={answer.label}>
            <td>{answer.label}</td>
            <td>
              <span className="glyph-word">
                <StatusGlyph kind={answer.allowed ? "succeeded" : "failed"} />
                {t(answer.allowed ? "table.access.allowed" : "table.access.refused")}
              </span>
            </td>
            <td>
              <VisibleText text={answer.words} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Properties of {@link ListSection}. */
interface ListSectionProps<T> {
  /** The heading. */
  readonly heading: ReactNode;
  /** The heading's level: 2 for a section of the tab, 3 for a level of grants. */
  readonly level: 2 | 3;
  /** What the read answered. */
  readonly resource: Resource<T>;
  /** What to say while it is read. */
  readonly reading: string;
  /** The answer, shown once it arrived. */
  readonly children: (data: T) => ReactNode;
}

/**
 * A section with its heading, the answer of one read, and while it is read a line that says so.
 * A failed read says why beside Try again and leaves the tab's other sections as they are; once
 * it succeeds the focus goes to the heading, because the button is gone.
 *
 * @template T - What the read answers.
 * @param props - The heading, the read and how to show its answer.
 * @param props.heading - The heading.
 * @param props.level - The heading's level.
 * @param props.resource - What the read answered.
 * @param props.reading - What to say while it is read.
 * @param props.children - Shows the answer.
 * @returns The section.
 */
function ListSection<T>({
  heading,
  level,
  resource,
  reading,
  children,
}: ListSectionProps<T>): JSX.Element {
  const { target, arm } = useFocusAfter<HTMLHeadingElement>(resource.data);
  const Heading = level === 2 ? "h2" : "h3";
  let shown: ReactNode;
  if (resource.error !== null) {
    shown = (
      <ReadFailed
        message={resource.error}
        onRetry={() => {
          arm();
          resource.reload();
        }}
      />
    );
  } else if (resource.data === undefined) {
    shown = <p role="status">{reading}</p>;
  } else {
    shown = children(resource.data);
  }
  return (
    <section className={level === 2 ? "access-section flat-panel" : "access-section"}>
      <Heading
        className={level === 2 ? "block-heading" : "block-subheading"}
        tabIndex={-1}
        ref={target}
      >
        {heading}
      </Heading>
      {shown}
    </section>
  );
}

/**
 * What the person may do, from the permissions the page already read beside the table.
 *
 * @returns The section.
 */
function Mine(): JSX.Element {
  const { t } = useTranslation();
  const { permissions } = useTableData();
  return (
    <ListSection
      heading={t("table.access.mine.heading")}
      level={2}
      resource={permissions}
      reading={t("table.access.mine.reading")}
    >
      {(answer) => <Answers permissions={answer.permissions} />}
    </ListSection>
  );
}

/** The command that lists every grant on the whole instance. */
const INSTANCE_GRANTS_COMMAND = "lakana grant list --kind INSTANCE";

/** Properties of {@link GrantsAt}. */
interface GrantsAtProps {
  /** The level whose grants are listed: an object, or `instance` for the whole instance. */
  readonly level: GrantLevel | "instance";
  /** The level's heading, such as "On namespace gold". */
  readonly heading: ReactNode;
}

/**
 * The grants on one level, the first page of at most 200; when the server has more, the CLI
 * command that lists them all.
 *
 * @param props - The level.
 * @param props.level - The level whose grants are listed.
 * @param props.heading - The level's heading.
 * @returns The level's section.
 */
function GrantsAt({ level, heading }: GrantsAtProps): JSX.Element {
  const { t } = useTranslation();
  const instance = level === "instance";
  const environment = instance ? "" : level.environment;
  const namespace = instance ? null : level.namespace;
  const table = instance ? null : level.table;
  const load = useCallback(
    () => (instance ? listInstanceGrants() : listGrantsAt({ environment, namespace, table })),
    [instance, environment, namespace, table],
  );
  const resource = useResource(load);
  return (
    <ListSection
      heading={heading}
      level={3}
      resource={resource}
      reading={t("table.access.who.reading")}
    >
      {({ grants, more }) =>
        grants.length === 0 ? (
          <p className="second">{t("table.access.who.none")}</p>
        ) : (
          <>
            <table className="list list-static">
              <thead>
                <tr>
                  <th scope="col">{t("table.access.who.subject")}</th>
                  <th scope="col">{t("table.access.who.grant")}</th>
                  <th scope="col">{t("table.access.who.since")}</th>
                </tr>
              </thead>
              <tbody>
                {grants.map((grant) => (
                  <tr key={grant.id}>
                    <td>
                      <SubjectName subject={grant} link />
                    </td>
                    <td>
                      {/* A grant gives a role or a single action, never both. */}
                      {grant.role !== null &&
                        t("table.access.who.role", {
                          role: t(`table.access.roles.${grant.role}`),
                        })}
                      {grant.action !== null && <VisibleText text={grant.action} breakable />}
                    </td>
                    <td>
                      <When iso={grant.granted_at} never="" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {more && (
              <p className="second grants-cut">
                <Trans
                  t={t}
                  i18nKey="table.access.who.cut"
                  components={{
                    command: (
                      <code>
                        <VisibleText
                          text={
                            instance
                              ? INSTANCE_GRANTS_COMMAND
                              : grantListCommand(environment, namespace, table)
                          }
                        />
                      </code>
                    ),
                  }}
                />
              </p>
            )}
          </>
        )
      }
    </ListSection>
  );
}

/**
 * Who has access: the grants on the table, on its namespace and each one above it, on the
 * environment, and on the whole instance, each level read on its own.
 *
 * @returns The section.
 */
function WhoHasAccess(): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const { environment, namespace, name } = ref;
  return (
    <section className="access-section flat-panel">
      <h2 className="block-heading">{t("table.access.who.heading")}</h2>
      <GrantsAt
        level={{ environment, namespace, table: name }}
        heading={t("table.access.who.table")}
      />
      {namespaceLevels(namespace).map((level) => (
        <GrantsAt
          key={level}
          level={{ environment, namespace: level, table: null }}
          heading={
            <Trans
              t={t}
              i18nKey="table.access.who.namespace"
              components={{ name: <VisibleText text={level} /> }}
            />
          }
        />
      ))}
      <GrantsAt
        level={{ environment, namespace: null, table: null }}
        heading={
          <Trans
            t={t}
            i18nKey="table.access.who.environment"
            components={{ name: <VisibleText text={environment} /> }}
          />
        }
      />
      <GrantsAt level="instance" heading={t("table.access.who.instance")} />
      <p>
        <Link to={grantsPath({ where: { kind: "TABLE", environment, namespace, table: name } })}>
          {t("table.access.manage")}
        </Link>
      </p>
    </section>
  );
}

/** The most row filters or column masks the tab reads of a table: the server's largest page. */
const MOST_POLICIES = 200;

/**
 * The policy lists' filter of exactly one table.
 *
 * @param environment - The table's environment.
 * @param namespace - Its dotted namespace.
 * @param table - Its name.
 * @returns The filter.
 */
function tableFilter(environment: string, namespace: string, table: string): PolicyFilter {
  return { who: null, environment, namespace, table };
}

/**
 * A list of the table's policies, and the sentence that says when the server holds more.
 *
 * @param props - The header cells, the rows and whether there are more.
 * @param props.head - The header row's cells.
 * @param props.more - Whether the server named a next page.
 * @param props.children - The rows.
 * @returns The list.
 */
function PolicyTable({
  head,
  more,
  children,
}: {
  readonly head: ReactNode;
  readonly more: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      <table className="list list-static">
        <thead>
          <tr>{head}</tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
      {more && <p className="second">{t("table.access.policiesCut")}</p>}
    </>
  );
}

/**
 * A policy's expression, as code.
 *
 * @param props - The expression.
 * @param props.expression - The SQL expression.
 * @returns The cell.
 */
function ExpressionCell({ expression }: { readonly expression: string }): JSX.Element {
  return (
    <td>
      <code className="expression">
        <VisibleText text={expression} breakable />
      </code>
    </td>
  );
}

/**
 * The table's row filters: who each is for and the rows it shows them.
 *
 * @returns The section.
 */
function RowFilters(): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const { environment, namespace, name } = ref;
  const load = useCallback(
    () => listRowFilterPage(tableFilter(environment, namespace, name), null, MOST_POLICIES),
    [environment, namespace, name],
  );
  const resource = useResource<CursorPage<RowFilterOut>>(load);
  return (
    <ListSection
      heading={t("table.access.filters.heading")}
      level={2}
      resource={resource}
      reading={t("table.access.filters.reading")}
    >
      {({ items: filters, nextCursor }) =>
        filters.length === 0 ? (
          <p className="second">{t("table.access.filters.none")}</p>
        ) : (
          <PolicyTable
            more={nextCursor !== null}
            head={
              <>
                <th scope="col">{t("table.access.who.subject")}</th>
                <th scope="col">{t("table.access.filters.expression")}</th>
              </>
            }
          >
            {filters.map((filter) => (
              <tr key={filter.id}>
                <td>
                  <SubjectName subject={filter} link />
                </td>
                <ExpressionCell expression={filter.expression} />
              </tr>
            ))}
          </PolicyTable>
        )
      }
    </ListSection>
  );
}

/**
 * The table's column masks: who each is for, the column and what they see instead.
 *
 * @returns The section.
 */
function ColumnMasks(): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const { environment, namespace, name } = ref;
  const load = useCallback(
    () => listColumnMaskPage(tableFilter(environment, namespace, name), null, MOST_POLICIES),
    [environment, namespace, name],
  );
  const resource = useResource<CursorPage<ColumnMaskOut>>(load);
  return (
    <ListSection
      heading={t("table.access.masks.heading")}
      level={2}
      resource={resource}
      reading={t("table.access.masks.reading")}
    >
      {({ items: masks, nextCursor }) =>
        masks.length === 0 ? (
          <p className="second">{t("table.access.masks.none")}</p>
        ) : (
          <PolicyTable
            more={nextCursor !== null}
            head={
              <>
                <th scope="col">{t("table.access.who.subject")}</th>
                <th scope="col">{t("table.access.masks.column")}</th>
                <th scope="col">{t("table.access.masks.expression")}</th>
              </>
            }
          >
            {masks.map((mask) => (
              <tr key={mask.id}>
                <td>
                  <SubjectName subject={mask} link />
                </td>
                <td>
                  <VisibleText text={mask.column} breakable name />
                </td>
                <ExpressionCell expression={mask.expression} />
              </tr>
            ))}
          </PolicyTable>
        )
      }
    </ListSection>
  );
}

/**
 * The Access tab. Everyone sees what they may do; only an administrator also reads and sees who
 * has access, the row filters and the column masks; no one else asks for them.
 *
 * @returns The tab's content.
 */
export default function AccessTab(): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const { ref } = useTableData();
  return (
    <div className="access-tab">
      <Mine />
      {me.admin && (
        <>
          <WhoHasAccess />
          <RowFilters />
          <ColumnMasks />
          <p>
            <Link
              to={policiesPath({
                environment: ref.environment,
                namespace: ref.namespace,
                table: ref.name,
              })}
            >
              {t("table.access.managePolicies")}
            </Link>
          </p>
        </>
      )}
      {!me.admin && <p className="second">{t("table.access.later")}</p>}
    </div>
  );
}
