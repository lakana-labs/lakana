/**
 * Settings › Access › Row filters & masks: every row filter and column mask, filtered by
 * environment, table and who, with Delete and Add filter or mask.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { listEnvironments } from "../api/environments";
import { NO_POLICY_FILTER, type PolicyFilter, type PolicyKind } from "../api/policies";
import { ReadFailed } from "../catalog/ReadFailed";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { useResource } from "../shell/useResource";
import "./access.css";
import { policiesPath, policyFilterKey, readPolicyFilter } from "./accessPaths";
import "./accessStrings";
import { managedScopeOf } from "./managed";
import { ManagedNote } from "./ManagedNote";
import "./policies.css";
import { PolicyFilters } from "./PolicyFilters";
import { PolicyList } from "./PolicyList";
import "./policyStrings";
import { useManagedScopes } from "./useManagedScopes";
import type { FilterStep } from "./WhoFilter";

// Add filter or mask's form loads when it is opened, not with the lists.
const AddPolicyDialog = lazy(() =>
  import("./AddPolicyDialog").then((module) => ({ default: module.AddPolicyDialog })),
);

/** The two lists, in the order they are shown. */
const KINDS: readonly PolicyKind[] = ["row-filter", "column-mask"];

/** Properties of {@link PolicySection}. */
interface PolicySectionProps {
  /** Which list. */
  readonly kind: PolicyKind;
  /** The filter. */
  readonly filter: PolicyFilter;
  /** The managed scopes, or null until read. */
  readonly scopes: Parameters<typeof PolicyList>[0]["scopes"];
  /** Changes when the list must be read again. */
  readonly round: number;
  /** Called once a policy is deleted. */
  readonly onDeleted: () => void;
  /** Goes back to every policy, with the focus on the page's heading. */
  readonly onClear: () => void;
}

/**
 * One list in its flat panel, under its heading, which takes the focus after a deletion.
 *
 * @param props - The kind, the filter, the scopes, the round and the handlers.
 * @param props.kind - Which list.
 * @param props.filter - The filter.
 * @param props.scopes - The managed scopes.
 * @param props.round - Changes when the list must be read again.
 * @param props.onDeleted - Called once a policy is deleted.
 * @param props.onClear - Goes back to every policy.
 * @returns The section.
 */
function PolicySection({
  kind,
  filter,
  scopes,
  round,
  onDeleted,
  onClear,
}: PolicySectionProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useRef<HTMLHeadingElement>(null);
  return (
    // Not a region of its own: the list's scrolling region carries the section's name.
    <section className="flat-panel access-section">
      <h2 ref={heading} tabIndex={-1}>
        {t(`policies.${kind}.title`)}
      </h2>
      <PolicyList
        key={policyFilterKey(filter)}
        kind={kind}
        filter={filter}
        scopes={scopes}
        round={round}
        headingRef={heading}
        onDeleted={onDeleted}
        onClear={onClear}
      />
    </section>
  );
}

/**
 * Renders the filter (kept in the address), the two lists under the filter's key, and the one
 * action, Add filter or mask. Until the managed scopes are read nothing can be changed; where
 * an access file manages a policy's environment, the policy carries the Git mark instead of
 * Delete, and a managed instance shows the note with the emergency commands and offers no
 * change at all.
 *
 * @returns The place.
 */
export function FiltersAndMasksPage(): JSX.Element {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const filter = readPolicyFilter(params);
  const managed = useManagedScopes();
  const loadEnvironments = useCallback(() => listEnvironments(), []);
  const environments = useResource(loadEnvironments);
  const heading = useRef<HTMLHeadingElement>(null);
  const [note, setNote] = useState("");
  const [rounds, setRounds] = useState<Record<PolicyKind, number>>({
    "row-filter": 0,
    "column-mask": 0,
  });
  const [adding, setAdding] = useState(false);
  const scopes = managed.data ?? null;
  const choices = environments.data ?? [];
  const managedInstance = scopes === null ? undefined : managedScopeOf(scopes, null);
  const go = (next: PolicyFilter, step: FilterStep): void => {
    setParams(new URLSearchParams(policiesPath(next).split("?")[1] ?? ""), {
      replace: step === "replace",
    });
  };
  return (
    <SettingsFrame
      title={t("settings.places.filtersAndMasks")}
      headingRef={heading}
      action={
        managedInstance === null && (
          <Button
            className="button button-primary"
            onPress={() => {
              setAdding(true);
            }}
          >
            {t("policies.add")}
          </Button>
        )
      }
    >
      <p className="page-purpose">{t("policies.intro")}</p>
      {managed.error !== null && (
        <ReadFailed
          message={t("access.managed.readFailed", { sentence: managed.error })}
          onRetry={() => {
            heading.current?.focus();
            managed.reload();
          }}
        />
      )}
      {managedInstance !== undefined && managedInstance !== null && (
        <ManagedNote
          scope={managedInstance}
          commands={[t("access.managed.filterCommand"), t("access.managed.maskCommand")]}
        />
      )}
      <PolicyFilters filter={filter} environments={choices} onChange={go} />
      <p className="step-status" role="status">
        {note}
      </p>
      {KINDS.map((kind) => (
        <PolicySection
          key={kind}
          kind={kind}
          filter={filter}
          scopes={scopes}
          round={rounds[kind]}
          onDeleted={() => {
            setNote(t("policies.deleted"));
          }}
          onClear={() => {
            heading.current?.focus();
            go(NO_POLICY_FILTER, "push");
          }}
        />
      ))}
      {adding && scopes !== null && (
        <Suspense fallback={null}>
          <AddPolicyDialog
            start={filter}
            environments={choices}
            scopes={scopes}
            onClose={() => {
              setAdding(false);
            }}
            onCreated={(kind) => {
              setAdding(false);
              setNote(t(`policies.${kind}.created`));
              setRounds((previous) => ({ ...previous, [kind]: previous[kind] + 1 }));
            }}
          />
        </Suspense>
      )}
    </SettingsFrame>
  );
}
