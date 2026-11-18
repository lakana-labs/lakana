/**
 * Settings › Access › What I may do: one's own grants, direct and through groups, and what they
 * add up to on the instance and in each environment.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { listEnvironments } from "../api/environments";
import { type HeldGrantOut, listHeldGrants } from "../api/grants";
import { myPermissions, type PermissionsOut } from "../api/table";
import { ReadFailed } from "../catalog/ReadFailed";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { useMe } from "../shell/me";
import { useResource } from "../shell/useResource";
import { problemText } from "../sign-in/messages";
import "./access.css";
import "./accessStrings";
import { HeldGrants } from "./HeldGrants";
import { actionWords, isAction } from "./words";

/** How many environments are asked about at once. */
const READS_IN_FLIGHT = 4;

/** One scope's read: on its way, its answer, or why it failed. */
type ScopeRead =
  | { readonly state: "reading"; readonly again: boolean }
  | { readonly state: "read"; readonly again: boolean; readonly answer: PermissionsOut }
  | { readonly state: "failed"; readonly again: boolean; readonly error: string };

/** The key of the whole instance among the scopes; no environment's name is empty. */
const INSTANCE = "";

/**
 * Runs `work` over `items`, at most `limit` at once.
 *
 * @param items - The items.
 * @param limit - How many run at once.
 * @param work - What runs for each; it never rejects.
 */
async function inPool(
  items: readonly string[],
  limit: number,
  work: (item: string) => Promise<void>,
): Promise<void> {
  const queue = [...items];
  const worker = async (): Promise<void> => {
    for (let item = queue.shift(); item !== undefined; item = queue.shift()) {
      await work(item);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, queue.length) }, worker));
}

/**
 * Reads what a person may do on the instance and in each environment, a few at a time, and
 * reads one scope again on request.
 *
 * @param person - The person's name.
 * @param scopes - The instance ({@link INSTANCE}) and the environments, in order; null until
 *   they are known.
 * @returns Each scope's read, and the way to read one again.
 */
function usePermissions(
  person: string,
  scopes: readonly string[] | null,
): {
  readonly reads: ReadonlyMap<string, ScopeRead>;
  readonly retry: (scope: string) => void;
} {
  const { t } = useTranslation();
  const [reads, setReads] = useState<ReadonlyMap<string, ScopeRead>>(new Map());
  const read = useCallback(
    async (scope: string, again = false): Promise<void> => {
      setReads((previous) => new Map(previous).set(scope, { state: "reading", again }));
      let next: ScopeRead;
      try {
        const answer = await myPermissions(
          person,
          scope === INSTANCE ? undefined : { environment: scope },
        );
        next = { state: "read", again, answer };
      } catch (failure: unknown) {
        next = { state: "failed", again, error: problemText(t, failure) };
      }
      // After the page is gone, React drops the update.
      setReads((previous) => new Map(previous).set(scope, next));
    },
    [person, t],
  );
  const key = scopes === null ? null : scopes.join("\n");
  useEffect(() => {
    if (key === null) return;
    void inPool(key.split("\n"), READS_IN_FLIGHT, (scope) => read(scope));
  }, [key, read]);
  const retry = useCallback(
    (scope: string) => {
      void read(scope, true);
    },
    [read],
  );
  return { reads, retry };
}

/** Properties of {@link ScopeSection}. */
interface ScopeSectionProps {
  /** The environment, or {@link INSTANCE}. */
  readonly scope: string;
  /** Its read. */
  readonly read: ScopeRead | undefined;
  /** Reads it again. */
  readonly onRetry: () => void;
}

/**
 * One scope's part: its name, then the actions allowed there in words, in the server's order.
 * A scope with none is left out, unless it was read again after a failure (the focus is on its
 * heading then); one that could not be read keeps its place with the sentence and Try again,
 * which leaves the focus on its heading.
 *
 * @param props - The scope, its read and the retry.
 * @param props.scope - The environment, or the instance.
 * @param props.read - Its read.
 * @param props.onRetry - Reads it again.
 * @returns The part, or nothing.
 */
function ScopeSection({ scope, read, onRetry }: ScopeSectionProps): JSX.Element | null {
  const { t } = useTranslation();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  if (read === undefined || (read.state === "reading" && !read.again)) return null;
  const allowed =
    read.state === "read"
      ? read.answer.permissions
          .filter((permission) => permission.allowed)
          .map((permission) => permission.action)
      : [];
  if (read.state === "read" && allowed.length === 0 && !read.again) return null;
  return (
    <section aria-labelledby={id}>
      <h3 id={id} tabIndex={-1} ref={heading}>
        {scope === INSTANCE ? (
          t("access.whatIMayDo.instance")
        ) : (
          <EnvironmentLabel environment={scope} />
        )}
      </h3>
      {read.state === "reading" && (
        <p className="step-status" role="status">
          {t("access.whatIMayDo.reading")}
        </p>
      )}
      {read.state === "read" && allowed.length === 0 && (
        <p className="empty">{t("access.whatIMayDo.nothing")}</p>
      )}
      {read.state === "failed" && (
        <ReadFailed
          message={
            scope === INSTANCE
              ? t("access.whatIMayDo.readFailedInstance", { sentence: read.error })
              : t("access.whatIMayDo.readFailed", { environment: scope, sentence: read.error })
          }
          onRetry={() => {
            heading.current?.focus();
            onRetry();
          }}
        />
      )}
      {allowed.length > 0 && (
        <ul className="access-actions">
          {allowed.map((action) => (
            <li key={action}>{isAction(action) ? actionWords(t, action) : action}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * The environments to ask about: those the person may read and those a grant names, by name.
 *
 * @param readable - The environments the person may read.
 * @param grants - The person's grants.
 * @returns The names, sorted, once each.
 */
function environmentsOf(readable: readonly string[], grants: readonly HeldGrantOut[]): string[] {
  const named = grants.flatMap((grant) => (grant.environment === null ? [] : [grant.environment]));
  return [...new Set([...readable, ...named])].sort((a, b) => a.localeCompare(b));
}

/**
 * Renders the page: one's grants in a table (through which group or directly), then what they
 * add up to, scope by scope. Read once, not polled: grants change only when someone acts.
 *
 * @returns The place.
 */
export function WhatIMayDo(): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const grantsId = useId();
  const addsUpId = useId();
  const grantsHeading = useRef<HTMLHeadingElement>(null);
  const addsUpHeading = useRef<HTMLHeadingElement>(null);
  const loadGrants = useCallback(() => listHeldGrants(me.name), [me.name]);
  const grants = useResource(loadGrants);
  const environments = useResource(listEnvironments);
  const held = grants.data;
  const any = held !== undefined && held.length > 0;
  const scopes =
    any && environments.data !== undefined
      ? [
          INSTANCE,
          ...environmentsOf(
            environments.data.map((environment) => environment.name),
            held,
          ),
        ]
      : null;
  const { reads, retry } = usePermissions(me.name, scopes);
  const settled =
    scopes !== null &&
    scopes.every((scope) => {
      const state = reads.get(scope)?.state;
      return state !== undefined && state !== "reading";
    });
  const nothing =
    settled &&
    scopes.every((scope) => {
      const read = reads.get(scope);
      return (
        read?.state === "read" && !read.answer.permissions.some((permission) => permission.allowed)
      );
    });
  return (
    <SettingsFrame title={t("settings.places.whatIMayDo")}>
      <p className="page-purpose">{t("access.whatIMayDo.intro")}</p>
      <section className="flat-panel access-section" aria-labelledby={grantsId}>
        <h2 id={grantsId} tabIndex={-1} ref={grantsHeading}>
          {t("access.whatIMayDo.grants")}
        </h2>
        {grants.error !== null && (
          <ReadFailed
            message={grants.error}
            onRetry={() => {
              grantsHeading.current?.focus();
              grants.reload();
            }}
          />
        )}
        {held === undefined ? (
          grants.error === null && <p className="step-status">{t("access.whatIMayDo.reading")}</p>
        ) : held.length === 0 ? (
          <p className="empty">{t("access.whatIMayDo.none")}</p>
        ) : (
          <HeldGrants grants={held} label={t("access.whatIMayDo.region")} linkGroups={me.admin} />
        )}
      </section>
      {any && (
        <section className="flat-panel access-section" aria-labelledby={addsUpId}>
          <h2 id={addsUpId} tabIndex={-1} ref={addsUpHeading}>
            {t("access.whatIMayDo.addsUp")}
          </h2>
          {environments.error !== null && (
            <ReadFailed
              message={t("access.whatIMayDo.environmentsFailed", {
                sentence: environments.error,
              })}
              onRetry={() => {
                addsUpHeading.current?.focus();
                environments.reload();
              }}
            />
          )}
          {!settled && environments.error === null && (
            <p className="step-status" role="status">
              {t("access.whatIMayDo.reading")}
            </p>
          )}
          {nothing && <p className="empty">{t("access.whatIMayDo.nothing")}</p>}
          {scopes?.map((scope) => (
            <ScopeSection
              key={scope}
              scope={scope}
              read={reads.get(scope)}
              onRetry={() => {
                retry(scope);
              }}
            />
          ))}
        </section>
      )}
    </SettingsFrame>
  );
}
