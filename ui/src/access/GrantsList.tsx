/**
 * The Grants list: the grants a filter names, a page at a time, each with Remove, or the Git
 * mark where an access file manages its scope.
 *
 * @packageDocumentation
 */
import { type JSX, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { ManagedScopeOut } from "../api/accessFile";
import { type GrantFilter, listGrants, NO_FILTER, revokeGrant } from "../api/grants";
import type { GrantOut } from "../api/table";
import { useCursorPages } from "../catalog/views/useCursorPages";
import { ViewList } from "../catalog/views/ViewList";
import { Avatar } from "../profile/Avatar";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import "./accessStrings";
import { ClearFilters, ManagedMark } from "./ListBits";
import { managedScopeOf } from "./managed";
import { filterKey, groupPath } from "./accessPaths";
import { personPath } from "./personPath";
import { whatWords, whereText } from "./words";

/** Properties of {@link GrantsList}. */
export interface GrantsListProps {
  /** The filter. */
  readonly filter: GrantFilter;
  /** The managed scopes; null until they are read, when no grant offers Remove. */
  readonly scopes: readonly ManagedScopeOut[] | null;
  /** Changes when the list must be read again from its first page, such as after Add grant. */
  readonly round: number;
  /** Called once a grant is removed. */
  readonly onRemoved: () => void;
  /** Goes back to every grant. */
  readonly onClear: () => void;
  /** The page's heading, which takes the focus once a removed grant has left the list. */
  readonly headingRef: RefObject<HTMLHeadingElement | null>;
}

/**
 * Who a grant is for, in words, for a sentence or an accessible name.
 *
 * @param t - The translation function.
 * @param grant - The grant.
 * @returns The name.
 */
function whoText(t: ReturnType<typeof useTranslation>["t"], grant: GrantOut): string {
  return grant.group ?? grant.principal?.name ?? t("access.grants.unnamed");
}

/** Properties of {@link Who}. */
interface WhoProps {
  /** The grant. */
  readonly grant: GrantOut;
}

/**
 * The cell that says who a grant is for: the group or the person, as a link to their page, and
 * what they are as the second line.
 *
 * @param props - The grant.
 * @param props.grant - The grant.
 * @returns The cell's content.
 */
function Who({ grant }: WhoProps): JSX.Element {
  const { t } = useTranslation();
  if (grant.group !== null) {
    return (
      <>
        <Link className="access-name" to={groupPath(grant.group)}>
          <VisibleText text={grant.group} />
        </Link>
        <span className="second">{t("access.grants.group")}</span>
      </>
    );
  }
  const principal = grant.principal;
  if (principal === null) {
    return <span className="access-name">{t("access.grants.unnamed")}</span>;
  }
  return (
    <div className="person-cell">
      <Avatar name={principal.name} displayName={null} etag={null} size={24} />
      <Link className="access-name" to={personPath(principal.name)}>
        <VisibleText text={principal.name} />
      </Link>
      <span className="second">
        {principal.principal_type === "user"
          ? t("access.people.person")
          : t("access.people.service")}
      </span>
    </div>
  );
}

/**
 * Renders the list for one filter; the page renders it under the filter's key, so a new filter
 * drops the old rows and the old error at once. A grant is a fact without a page: its row opens
 * nothing. Remove asks first (naming who, what and where), then the passkey touch; the server's
 * refusal (the last administrator's grant) stays in the dialog. After a removal the list is
 * read again from its first page and the focus goes to the page's heading.
 *
 * @param props - The filter, the managed scopes, the round and the handlers.
 * @param props.filter - The filter.
 * @param props.scopes - The managed scopes, or null.
 * @param props.round - Changes when the list must be read again.
 * @param props.onRemoved - Called once a grant is removed.
 * @param props.onClear - Goes back to every grant.
 * @param props.headingRef - The page's heading.
 * @returns The list.
 */
export function GrantsList({
  filter,
  scopes,
  round,
  onRemoved,
  onClear,
  headingRef,
}: GrantsListProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const load = useCallback((cursor: string | null) => listGrants(filter, cursor), [filter]);
  const pages = useCursorPages(filterKey(filter), load);
  const { reload } = pages;
  const [removing, setRemoving] = useState<GrantOut | null>(null);
  // After a removal: "removed" until the list is read again, "reading" while it is.
  const focusHeading = useRef<"removed" | "reading" | null>(null);
  const firstRound = useRef(round);
  useEffect(() => {
    if (round !== firstRound.current) reload();
  }, [round, reload]);
  const rows = pages.items ?? [];
  useEffect(() => {
    if (focusHeading.current === "removed" && pages.busy) {
      focusHeading.current = "reading";
    } else if (focusHeading.current === "reading" && !pages.busy) {
      focusHeading.current = null;
      headingRef.current?.focus();
    }
  }, [pages.busy, headingRef]);
  const filtered = filterKey(filter) !== filterKey(NO_FILTER);
  const words = (grant: GrantOut): { who: string; what: string; where: string } => ({
    who: whoText(t, grant),
    what: whatWords(t, grant),
    where: whereText(t, grant),
  });
  return (
    <>
      <p className="step-status" role="status">
        {pages.busy && pages.items === undefined ? t("access.grants.reading") : ""}
      </p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={filtered ? t("access.grants.noMatch") : t("access.grants.none")}
        region={t("access.grants.region")}
        scroll="grants-scroll"
        head={
          <tr>
            <th scope="col">{t("access.grants.who")}</th>
            <th scope="col">{t("access.grants.what")}</th>
            <th scope="col">{t("access.grants.on")}</th>
            <th scope="col">{t("access.grants.grantedAt")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("list.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((grant) => {
          const managed = scopes === null ? null : managedScopeOf(scopes, grant.environment);
          return (
            <tr key={grant.id}>
              <td>
                <Who grant={grant} />
              </td>
              <td>
                <span className="access-name">{whatWords(t, grant)}</span>
                {grant.note !== null && <span className="second">{grant.note}</span>}
              </td>
              <td>{whereText(t, grant)}</td>
              <td>
                <time dateTime={grant.granted_at}>{when(grant.granted_at)}</time>
              </td>
              <td>
                {managed !== null ? (
                  <ManagedMark file={managed.file_name} />
                ) : (
                  scopes !== null && (
                    <Button
                      className="button"
                      aria-label={t("access.grants.removeOf", words(grant))}
                      onPress={() => {
                        setRemoving(grant);
                      }}
                    >
                      {t("access.grants.remove")}
                    </Button>
                  )
                )}
              </td>
            </tr>
          );
        })}
      </ViewList>
      <ClearFilters
        pages={pages}
        filtered={filtered}
        label={t("access.grants.clear")}
        onClear={onClear}
      />
      {removing !== null && (
        <ConfirmDialog
          title={t("access.grants.removeTitle")}
          confirmLabel={t("access.grants.removeConfirm")}
          onConfirm={() => revokeGrant(removing.id)}
          onCancel={() => {
            setRemoving(null);
          }}
          onDone={() => {
            setRemoving(null);
            focusHeading.current = "removed";
            onRemoved();
            reload();
          }}
        >
          <p>{t("access.grants.removeText", words(removing))}</p>
        </ConfirmDialog>
      )}
    </>
  );
}
