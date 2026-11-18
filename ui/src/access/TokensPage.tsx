/**
 * Settings › Access › Tokens: everyone's live personal tokens and the live task tokens, each
 * list with its own filter in the address, each token revocable. Minting stays on the command
 * line, and the page names the commands.
 *
 * @packageDocumentation
 */
import { type JSX, useCallback, useId, useRef, useState } from "react";
import { Input, Label, Text, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router";

import { listEnvironments } from "../api/environments";
import { SettingsFrame } from "../settings/SettingsFrame";
import "../settings/settingsStrings";
import { CopyLine } from "../shell/CopyLine";
import { EnvironmentPicker } from "../shell/EnvironmentPicker";
import { useResource } from "../shell/useResource";
import "./access.css";
import { tokensPath } from "./accessPaths";
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
 * Renders the place: Personal tokens with the Owner filter (applied on Enter or when the field
 * is left, `?owner=`) and the command for a service account's token, then Task tokens with the
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
  const applyOwner = (): void => {
    const next = typed.trim() === "" ? null : typed.trim();
    if (next !== owner) go({ owner: next, environment });
  };
  return (
    <SettingsFrame title={t("settings.places.tokens")}>
      <section className="flat-panel access-section" aria-labelledby={personalId}>
        <h2 id={personalId} ref={personalHeading} tabIndex={-1}>
          {t("tokens.personal.title")}
        </h2>
        <TextField
          className="field"
          value={typed}
          onChange={setTyped}
          onBlur={applyOwner}
          onKeyDown={(event) => {
            if (event.key === "Enter") applyOwner();
          }}
        >
          <Label>{t("tokens.personal.filter")}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t("tokens.personal.filterHelp")}</Text>
        </TextField>
        <PersonalTokens
          key={owner ?? ""}
          owner={owner}
          heading={personalHeading}
          onRevoked={() => {
            setTaskRound((previous) => previous + 1);
          }}
        />
        <p>{t("tokens.personal.serviceAccount")}</p>
        <CopyLine text={t("tokens.personal.issueCommand")} copyLabel={t("tokens.copyCommand")} />
      </section>
      <section className="flat-panel access-section" aria-labelledby={taskId}>
        <h2 id={taskId} ref={taskHeading} tabIndex={-1}>
          {t("tokens.task.title")}
        </h2>
        <EnvironmentPicker
          environments={environments.data ?? []}
          value={environment}
          label={t("tokens.task.environment")}
          all={t("tokens.task.all")}
          onChange={(next: string | null) => {
            go({ owner, environment: next });
          }}
        />
        <TaskTokens
          key={`${environment ?? ""}\n${String(taskRound)}`}
          environment={environment}
          heading={taskHeading}
        />
        <p>{t("tokens.task.mint")}</p>
        <CopyLine text={t("tokens.task.mintCommand")} copyLabel={t("tokens.copyCommand")} />
      </section>
    </SettingsFrame>
  );
}
