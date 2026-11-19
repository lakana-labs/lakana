/**
 * One of Row filters & masks' two lists: the row filters or the column masks a filter names, a
 * page at a time, each with Delete, or the Git mark where an access file manages its scope.
 *
 * @packageDocumentation
 */
import { type JSX, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { ManagedScopeBriefOut } from "../api/accessFile";
import {
  deleteColumnMask,
  deleteRowFilter,
  listColumnMaskPage,
  listRowFilterPage,
  NO_POLICY_FILTER,
  type PolicyFilter,
  type PolicyKind,
} from "../api/policies";
import type { CursorPage } from "../api/manage";
import type { ColumnMaskOut, RowFilterOut } from "../api/table";
import { tablePath } from "../catalog/paths";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { useMe } from "../shell/me";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { policyFilterKey } from "./accessPaths";
import "./accessStrings";
import { GitMark } from "./GitMark";
import { managedScopeOf } from "./managed";
import "./policyStrings";
import { SubjectName } from "./SubjectName";
import { whereText } from "./words";

/** A row filter or a column mask, as the lists hold them: a row filter has no column. */
type Policy = RowFilterOut & { readonly column: ColumnMaskOut["column"] | null };

/**
 * Reads one page of a kind of policy, each with its column (none for a row filter).
 *
 * @param kind - Which list.
 * @param filter - The filter.
 * @param cursor - The cursor of the page; null for the first.
 * @returns The page.
 */
async function policyPage(
  kind: PolicyKind,
  filter: PolicyFilter,
  cursor: string | null,
): Promise<CursorPage<Policy>> {
  if (kind === "column-mask") return listColumnMaskPage(filter, cursor);
  const page = await listRowFilterPage(filter, cursor);
  return { ...page, items: page.items.map((policy) => ({ ...policy, column: null })) };
}

/** Properties of {@link PolicyList}. */
export interface PolicyListProps {
  /** Which list: the row filters or the column masks. */
  readonly kind: PolicyKind;
  /** The filter. */
  readonly filter: PolicyFilter;
  /** The managed scopes; null until they are read, when no policy offers Delete. */
  readonly scopes: readonly ManagedScopeBriefOut[] | null;
  /** Changes when the list must be read again from its first page, such as after Add. */
  readonly round: number;
  /** The section's heading, which takes the focus once a deleted policy has left the list. */
  readonly headingRef: RefObject<HTMLHeadingElement | null>;
  /** Called once a policy is deleted. */
  readonly onDeleted: () => void;
  /** Goes back to every policy. */
  readonly onClear: () => void;
}

/**
 * Renders the list for one filter; the page renders it under the filter's key, so a new filter
 * drops the old rows and the old error at once. A policy is a fact without a page: its row
 * opens nothing, and its table links to the table's Access tab. Delete asks first, naming who
 * then sees more, then the passkey touch; a refusal or a cancelled touch stays in the dialog.
 * After a deletion the list is read again from its first page and the focus goes to the
 * section's heading.
 *
 * @param props - The kind, the filter, the managed scopes, the round, the heading and handlers.
 * @param props.kind - Which list.
 * @param props.filter - The filter.
 * @param props.scopes - The managed scopes, or null.
 * @param props.round - Changes when the list must be read again.
 * @param props.headingRef - The section's heading.
 * @param props.onDeleted - Called once a policy is deleted.
 * @param props.onClear - Goes back to every policy.
 * @returns The list.
 */
export function PolicyList({
  kind,
  filter,
  scopes,
  round,
  headingRef,
  onDeleted,
  onClear,
}: PolicyListProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const { admin } = useMe();
  const masks = kind === "column-mask";
  const load = useCallback(
    (cursor: string | null) => policyPage(kind, filter, cursor),
    [kind, filter],
  );
  const pages = useCursorPages(`${kind}\n${policyFilterKey(filter)}`, load);
  const { reload } = pages;
  const [deleting, setDeleting] = useState<Policy | null>(null);
  // After a deletion: "deleted" until the list is read again, "reading" while it is.
  const focusHeading = useRef<"deleted" | "reading" | null>(null);
  const firstRound = useRef(round);
  useEffect(() => {
    if (round !== firstRound.current) reload();
  }, [round, reload]);
  useEffect(() => {
    if (focusHeading.current === "deleted" && pages.busy) {
      focusHeading.current = "reading";
    } else if (focusHeading.current === "reading" && !pages.busy) {
      focusHeading.current = null;
      headingRef.current?.focus();
    }
  }, [pages.busy, headingRef]);
  const rows = pages.items ?? [];
  const filtered = policyFilterKey(filter) !== policyFilterKey(NO_POLICY_FILTER);
  const table = (policy: Policy): string => whereText(t, { kind: "TABLE", ...policy });
  const who = (policy: Policy): string =>
    policy.group ?? policy.principal?.name ?? t("policies.removed");
  return (
    <>
      <p className="step-status" role="status">
        {pages.busy && pages.items === undefined ? t(`policies.${kind}.reading`) : ""}
      </p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={filtered ? t(`policies.${kind}.noMatch`) : t(`policies.${kind}.none`)}
        region={t(`policies.${kind}.title`)}
        scroll="policies-scroll"
        head={
          <tr>
            <th scope="col">{t("policies.who")}</th>
            <th scope="col">{t("policies.on")}</th>
            {masks && <th scope="col">{t("policies.column")}</th>}
            <th scope="col">{t("policies.expression")}</th>
            <th scope="col">{t("policies.created")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((policy) => {
          const managed = scopes === null ? null : managedScopeOf(scopes, policy.environment);
          return (
            <tr key={policy.id}>
              <td>
                <SubjectName subject={policy} link={admin} />
              </td>
              <td>
                <Link
                  className="access-name"
                  to={tablePath(policy.environment, policy.namespace, policy.table, "access")}
                >
                  {table(policy)}
                </Link>
              </td>
              {policy.column !== null && (
                <td>
                  <span className="row-name">
                    <VisibleText text={policy.column} breakable />
                  </span>
                </td>
              )}
              <td>
                <code className="expression">
                  <VisibleText text={policy.expression} breakable />
                </code>
              </td>
              <td>
                <time dateTime={policy.created_at}>{when(policy.created_at)}</time>
              </td>
              <td>
                {managed !== null ? (
                  <>
                    <GitMark />
                    <span className="second">
                      {t("access.managed.markOf", { file: managed.file_name })}
                    </span>
                  </>
                ) : (
                  scopes !== null && (
                    <Button
                      className="button"
                      aria-label={
                        policy.column === null
                          ? t("policies.deleteFilterOf", {
                              table: table(policy),
                              who: who(policy),
                            })
                          : t("policies.deleteMaskOf", {
                              column: policy.column,
                              table: table(policy),
                              who: who(policy),
                            })
                      }
                      onPress={() => {
                        setDeleting(policy);
                      }}
                    >
                      {t("policies.delete")}
                    </Button>
                  )
                )}
              </td>
            </tr>
          );
        })}
      </ViewList>
      {filtered && pages.items !== undefined && rows.length === 0 && !pages.busy && (
        <p>
          <Button className="button" onPress={onClear}>
            {t("policies.clear")}
          </Button>
        </p>
      )}
      {deleting !== null && (
        <ConfirmDialog
          title={masks ? t("policies.deleteMaskTitle") : t("policies.deleteFilterTitle")}
          confirmLabel={masks ? t("policies.deleteMaskConfirm") : t("policies.deleteFilterConfirm")}
          onConfirm={() => (masks ? deleteColumnMask(deleting.id) : deleteRowFilter(deleting.id))}
          onCancel={() => {
            setDeleting(null);
          }}
          onDone={() => {
            setDeleting(null);
            focusHeading.current = "deleted";
            onDeleted();
            reload();
          }}
        >
          <p>
            <Trans
              t={t}
              i18nKey={masks ? "policies.deleteMaskText" : "policies.deleteFilterText"}
              components={{
                who: <VisibleText text={who(deleting)} />,
                table: (
                  <VisibleText
                    text={`${deleting.environment} › ${deleting.namespace} › ${deleting.table}`}
                  />
                ),
                ...(deleting.column === null
                  ? {}
                  : { column: <VisibleText text={deleting.column} /> }),
              }}
            />
          </p>
        </ConfirmDialog>
      )}
    </>
  );
}
