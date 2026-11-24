/**
 * Settings › Access › Tokens: everyone's live personal tokens and the live task tokens, each
 * list with its own filter in the address, each token revocable. Minting stays on the command
 * line, and the page names the commands.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { listEnvironments } from "../api/environments";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { useSettingsViewer } from "../settings/viewer";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { countWords, ListToolbar, useListState } from "../shell/ListToolbar";
import { NamePicker } from "../shell/NamePicker";
import { useResource } from "../shell/useResource";
import "./access.css";
import { tokensPath } from "./accessPaths";
import { principalSearch } from "./suggest";
import { PersonalTokens, TaskTokens } from "./TokenLists";
import "./tokenStrings";

/**
 * A filter's value from the address, unless it is missing or empty.
 *
 * @param params - The address's query.
 * @param name - The parameter.
 * @returns The value, or null.
 */
function filterOf(params: URLSearchParams, name: string): string | null {
  const value = params.get(name);
  return value === null || value === "" ? null : value;
}

/**
 * Renders the place: Personal tokens with the Owner filter (it suggests principals as one types;
 * applied when one is chosen, on Enter or when the field is left, `?owner=`) and the command for a service account's token, then Task tokens with the
 * Environment filter (`?environment=`) and the command that mints one. Each list is read under
 * its filter, so a new filter drops the old rows at once. A personal token's revoke reads the
 * task tokens again too: those it minted ended with it.
 *
 * @returns The place.
 */
export function TokensPage(): JSX.Element {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const owner = filterOf(params, "owner");
  const environment = filterOf(params, "environment");
  const [typed, setTyped] = useState(owner ?? "");
  const [typedFor, setTypedFor] = useState(owner);
  // An owner from elsewhere (Back, a person's page) sets the field again.
  if (typedFor !== owner) {
    setTypedFor(owner);
    setTyped(owner ?? "");
  }
  const [taskRound, setTaskRound] = useState(0);
  const [personalRound, setPersonalRound] = useState(0);
  const [personal, reportPersonal] = useListState();
  const [task, reportTask] = useListState();
  const loadEnvironments = useCallback(() => listEnvironments(), []);
  const environments = useResource(loadEnvironments);
  const personalHeading = useRef<HTMLHeadingElement>(null);
  const taskHeading = useRef<HTMLHeadingElement>(null);
  const personalId = useId();
  const taskId = useId();
  const go = (next: { owner: string | null; environment: string | null }): void => {
    const path = tokensPath({
      ...(next.owner === null ? {} : { owner: next.owner }),
      ...(next.environment === null ? {} : { environment: next.environment }),
    });
    setParams(new URLSearchParams(path.split("?")[1] ?? ""));
  };
  const applyOwner = (text: string = typed): void => {
    const next = text.trim() === "" ? null : text.trim();
    if (next !== owner) go({ owner: next, environment });
  };
  const admin = useSettingsViewer().admin;
  const suggest = useMemo(() => (admin ? principalSearch(t) : null), [admin, t]);
  return (
    <SettingsFrame title={t("settings.places.tokens")}>
      <section className="flat-panel access-section" aria-labelledby={personalId}>
        {/* The panel holds one list: its count is the panel's heading. */}
        <ListToolbar
          heading={t("tokens.personal.title")}
          headingId={personalId}
          headingRef={personalHeading}
          count={countWords(t, "tokens.personal", personal.shown, personal.more)}
          onRefresh={() => {
            setPersonalRound((previous) => previous + 1);
          }}
          refreshing={personal.busy}
        />
        <NamePicker
          label={t("tokens.personal.filter")}
          help={t("tokens.personal.filterHelp")}
          value={typed}
          onChange={setTyped}
          search={suggest}
          noMatch={(text) => t("picker.noPrincipal", { name: text })}
          onPick={(choice) => {
            applyOwner(choice.name);
          }}
          onBlur={() => {
            applyOwner();
          }}
          onEnter={() => {
            applyOwner();
          }}
        />
        <PersonalTokens
          key={owner ?? ""}
          round={personalRound}
          owner={owner}
          heading={personalHeading}
          onState={reportPersonal}
          onRevoked={() => {
            setTaskRound((previous) => previous + 1);
          }}
        />
        <p>{t("tokens.personal.serviceAccount")}</p>
        <CopyLine text={t("tokens.personal.issueCommand")} copyLabel={t("tokens.copyCommand")} />
      </section>
      <section className="flat-panel access-section" aria-labelledby={taskId}>
        <ListToolbar
          heading={t("tokens.task.title")}
          headingId={taskId}
          headingRef={taskHeading}
          count={countWords(t, "tokens.task", task.shown, task.more)}
          filters={
            <EnvironmentPicker
              environments={environments.data ?? []}
              value={environment}
              label={t("tokens.task.environment")}
              all={t("tokens.task.all")}
              onChange={(next: string | null) => {
                go({ owner, environment: next });
              }}
            />
          }
          onRefresh={() => {
            setTaskRound((previous) => previous + 1);
          }}
          refreshing={task.busy}
        />
        <TaskTokens
          key={environment ?? ""}
          round={taskRound}
          environment={environment}
          heading={taskHeading}
          onState={reportTask}
        />
        <p>{t("tokens.task.mint")}</p>
        <CopyLine text={t("tokens.task.mintCommand")} copyLabel={t("tokens.copyCommand")} />
      </section>
    </SettingsFrame>
  );
}
