/**
 * Settings › Access › Grants: every grant, filtered by who, on exactly what and what, with
 * Remove and Add grant.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { listEnvironments } from "../api/environments";
import { type GrantFilter, NO_FILTER } from "../api/grants";
import { ReadFailed } from "../catalog/ReadFailed";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { useResource } from "../shell/useResource";
import "./access.css";
import "./accessStrings";
import { type FilterStep, GrantFilters } from "./GrantFilters";
import { GrantsList } from "./GrantsList";
import { managedScopeOf } from "./managed";
import { ManagedNote } from "./ManagedNote";
import { filterKey, grantsPath, readGrantFilter } from "./paths";
import { useAccessStatus } from "./useAccessStatus";

/**
 * Renders the filters (kept in the address), the list under the filter's key, and the one
 * action, Add grant. Until the access files' status is read nothing can be changed; where an
 * access file manages a grant's scope, the grant carries the Git mark instead of Remove, and a
 * managed instance shows the note with the emergency command and offers no change at all.
 *
 * @returns The place.
 */
export function GrantsPage(): JSX.Element {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const filter = readGrantFilter(params);
  const status = useAccessStatus();
  const loadEnvironments = useCallback(() => listEnvironments(), []);
  const environments = useResource(loadEnvironments);
  const heading = useRef<HTMLHeadingElement>(null);
  const [note, setNote] = useState("");
  const [round] = useState(0);
  const scopes = status.data?.scopes ?? null;
  const managedInstance = scopes === null ? undefined : managedScopeOf(scopes, null);
  const go = (next: GrantFilter, step: FilterStep): void => {
    setParams(new URLSearchParams(grantsPath(next).split("?")[1] ?? ""), {
      replace: step === "replace",
    });
  };
  return (
    <SettingsFrame title={t("settings.places.grants")} headingRef={heading}>
      {status.error !== null && (
        <ReadFailed
          message={t("access.managed.readFailed", { sentence: status.error })}
          onRetry={() => {
            heading.current?.focus();
            status.reload();
          }}
        />
      )}
      {managedInstance !== undefined && managedInstance !== null && (
        <ManagedNote scope={managedInstance} command={t("access.managed.grantCommand")} />
      )}
      <GrantFilters filter={filter} environments={environments.data ?? []} onChange={go} />
      <p className="step-status" role="status">
        {note}
      </p>
      <GrantsList
        key={filterKey(filter)}
        filter={filter}
        scopes={scopes}
        round={round}
        headingRef={heading}
        onRemoved={() => {
          setNote(t("access.grants.removed"));
        }}
        onClear={() => {
          heading.current?.focus();
          go(NO_FILTER, "push");
        }}
      />
    </SettingsFrame>
  );
}
