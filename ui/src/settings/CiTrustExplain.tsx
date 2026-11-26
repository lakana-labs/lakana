/**
 * Explain a CI job's claims: which CI trust rule an exchange with an ID token's decoded claims
 * would match, and why the others do not.
 *
 * @remarks
 * It reads only: the server compares the claims as given, changes and records nothing, and
 * issues no token, so it asks for no passkey touch. The claims stay in the page's state: never
 * in the address, storage or a log. Text that looks like a token itself is refused before
 * anything is sent, since a token is a credential.
 * @packageDocumentation
 */
import { type JSX, useEffect, useId, useRef, useState } from "react";
import { Form, Input, Label, Text, TextArea, TextField } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { type CiTrustExplainOut, explainCiTrust, listCiTrustRules } from "../api/ciTrust";
import { type EnvironmentChoice, listEnvironments } from "../api/environments";
import { listRepositories } from "../api/repositories";
import { Choice } from "../access/Choice";
import { EnvironmentName } from "../shell/EnvironmentName";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import { problemText } from "../sign-in/messages";
import "../access/access.css";
import "./ciTrust.css";
import "./ciTrustStrings";
import { CI_TRUST_PATH, ciTrustRulePath, issuerWords } from "./ciTrustWords";
import "./settingsStrings";

const P = "settings.ciTrust.explain";

/** A JSON Web Token: three dot-separated base64url parts, the first a JSON header. */
const TOKEN = /^eyJ[\w-]*\.[\w-]+\.[\w-]*$/;

/** A JSON Web Token anywhere inside a text, such as a value that carries one. */
const TOKEN_INSIDE = /eyJ[\w-]*\.[\w-]+\.[\w-]*/;

/**
 * Whether a parsed value holds a token anywhere: in a text, a key, a list or an object inside.
 *
 * @param value - The value.
 * @returns The answer.
 */
function holdsToken(value: unknown): boolean {
  if (typeof value === "string") return TOKEN_INSIDE.test(value);
  if (Array.isArray(value)) return value.some(holdsToken);
  if (typeof value === "object" && value !== null) {
    return Object.entries(value).some(
      ([key, inner]) => TOKEN_INSIDE.test(key) || holdsToken(inner),
    );
  }
  return false;
}

/** Why pasted claims are not sent, in words. */
class ClaimsProblem extends Error {}

/** The claims as parsed, or the key of why they cannot be sent. */
type Parsed =
  | { readonly claims: Readonly<Record<string, unknown>> }
  | { readonly problem: "notJson" | "notObject" | "looksLikeToken" | "holdsToken" };

/**
 * Reads the pasted claims: one JSON object, never a token.
 *
 * @param text - What was pasted.
 * @returns The claims, or why not.
 */
function parseClaims(text: string): Parsed {
  const trimmed = text.trim();
  if (TOKEN.test(trimmed)) return { problem: "looksLikeToken" };
  let value: unknown;
  try {
    value = JSON.parse(trimmed);
  } catch {
    return { problem: "notJson" };
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { problem: "notObject" };
  }
  return holdsToken(value)
    ? { problem: "holdsToken" }
    : { claims: value as Record<string, unknown> };
}

/** Properties of {@link Report}. */
interface ReportProps {
  /** What the server answered. */
  readonly report: CiTrustExplainOut;
  /** The rules' names by id, as far as they are read. */
  readonly names: ReadonlyMap<string, string>;
}

/**
 * The verdict, as a pill and the server's sentence, what was compared, and every rule with
 * whether it matches and why not.
 *
 * @param props - The report and the rules' names.
 * @param props.report - What the server answered.
 * @param props.names - The rules' names by id.
 * @returns The report.
 */
function Report({ report, names }: ReportProps): JSX.Element {
  const { t } = useTranslation();
  const verdictId = useId();
  const rulesId = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, [report]);
  const accepted = report.verdict.startsWith("accepted");
  // The pill says accepted or refused, so the sentence beside it leaves that word out; an
  // accepting rule is named as in the list below.
  const acceptor =
    report.matching.length === 1 ? names.get(report.matching[0] as string) : undefined;
  const sentence = report.verdict.replace(/^(accepted|refused:?)\s*/, "");
  return (
    <>
      <section className="flat-panel settings-panel" aria-labelledby={verdictId}>
        <h2 id={verdictId} ref={heading} tabIndex={-1}>
          {t(`${P}.verdict`)}
        </h2>
        <p>
          <span className={`pill ${accepted ? "pill-ok" : "pill-bad"}`}>
            {accepted ? t(`${P}.accepted`) : t(`${P}.refused`)}
          </span>{" "}
          {accepted && acceptor !== undefined ? (
            t(`${P}.acceptedBy`, { name: acceptor })
          ) : (
            <VisibleText text={sentence} />
          )}
        </p>
        <p className="page-purpose">
          {t(`${P}.compared`, {
            issuer: issuerWords(t, report.issuer),
            environment: report.environment,
            repository: report.repository,
          })}
        </p>
      </section>
      <section className="flat-panel settings-panel ci-trust-links" aria-labelledby={rulesId}>
        <h2 id={rulesId}>{t(`${P}.rules`)}</h2>
        {report.rules.length === 0 ? (
          <p className="empty">{t(`${P}.noRules`)}</p>
        ) : (
          <ul className="ci-trust-explained">
            {report.rules.map((rule) => {
              const name = names.get(rule.rule_id);
              return (
                <li key={rule.rule_id}>
                  <p>
                    {name === undefined ? (
                      <span>{t(`${P}.removedRule`)}</span>
                    ) : (
                      <Link to={ciTrustRulePath(rule.rule_id)}>{name}</Link>
                    )}{" "}
                    <span className={`pill ${rule.matches ? "pill-ok" : "pill-neutral"}`}>
                      {rule.matches ? t(`${P}.matches`) : t(`${P}.noMatch`)}
                    </span>
                  </p>
                  {rule.reasons.length > 0 && (
                    <ul>
                      {rule.reasons.map((reason) => (
                        <li key={reason}>
                          <VisibleText text={reason} />
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
}

/**
 * Renders the form (the claims, the environment and the repository the job asks for, and the
 * issuer if the claims' `iss` is not the one to compare) and, once answered, the report. Explain
 * is held, saying why, until the claims, the environment and the repository are given; claims
 * that are no JSON object, or a token, are refused in words and nothing is sent.
 *
 * @returns The page.
 */
export function CiTrustExplain(): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const heldId = useId();
  const [claims, setClaims] = useState("");
  const [environment, setEnvironment] = useState<string | null>(null);
  const [repository, setRepository] = useState<string | null>(null);
  const [issuer, setIssuer] = useState("");
  const [report, setReport] = useState<CiTrustExplainOut | null>(null);
  const environments = useResource(listEnvironments);
  const repositories = useResource(listRepositories);
  const rules = useResource(listCiTrustRules);
  const names = new Map(
    (rules.data ?? []).map((rule) => [
      rule.id,
      t("settings.ciTrust.ruleName", {
        repository: rule.repository,
        environment: rule.environment,
      }),
    ]),
  );
  const needs: [boolean, string][] = [
    [claims.trim() === "", "claimsNeeded"],
    [environment === null, "environmentNeeded"],
    [repository === null, "repositoryNeeded"],
  ];
  const lacking = needs.find(([missing]) => missing)?.[1];
  const held = lacking === undefined ? null : t(`${P}.${lacking}`);
  const environmentList = environments.data ?? [];
  return (
    <Page
      title={t(`${P}.title`)}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: CI_TRUST_PATH, label: t("settings.places.ciTrust") },
      ]}
    >
      <p className="page-purpose">{t(`${P}.intro`)}</p>
      <Form
        className="flat-panel settings-panel ci-trust-explain"
        onSubmit={(event) => {
          event.preventDefault();
          attempt.run(
            async () => {
              const parsed = parseClaims(claims);
              if ("problem" in parsed) throw new ClaimsProblem(t(`${P}.${parsed.problem}`));
              setReport(
                await explainCiTrust({
                  claims: parsed.claims,
                  // Explain is held until both are chosen.
                  environment: environment as string,
                  repository: repository as string,
                  ...(issuer.trim() === "" ? {} : { issuer: issuer.trim() }),
                }),
              );
            },
            (error) => (error instanceof ClaimsProblem ? error.message : problemText(t, error)),
          );
        }}
      >
        <TextField className="field" value={claims} onChange={setClaims} isRequired>
          <Label>{t(`${P}.claims`)}</Label>
          <TextArea className="ci-trust-claims" autoComplete="off" spellCheck={false} />
          <Text slot="description">{t(`${P}.claimsHelp`)}</Text>
        </TextField>
        <Choice<string>
          label={t(`${P}.environment`)}
          value={environment}
          options={environmentList.map((choice) => ({
            id: choice.name,
            text: markedText(choice.name),
          }))}
          onChange={setEnvironment}
          render={(id) => (
            <EnvironmentName
              environment={
                // Only listed environments are options.
                environmentList.find((choice) => choice.name === id) as EnvironmentChoice
              }
            />
          )}
        />
        <Choice<string>
          label={t(`${P}.repository`)}
          value={repository}
          options={(repositories.data ?? []).map((choice) => ({
            id: choice.name,
            text: markedText(choice.name),
          }))}
          onChange={setRepository}
        />
        <TextField className="field" value={issuer} onChange={setIssuer}>
          <Label>{t(`${P}.issuer`)}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t(`${P}.issuerHelp`)}</Text>
        </TextField>
        <div className="step-actions">
          <PendingButton
            type="submit"
            className="button button-primary"
            pending={attempt.busy}
            isDisabled={held !== null}
            {...(held === null ? {} : { "aria-describedby": heldId })}
          >
            {t(`${P}.submit`)}
          </PendingButton>
        </div>
        {held !== null && (
          <p className="step-status" id={heldId}>
            {held}
          </p>
        )}
        <p className="step-status" role="status">
          {attempt.busy ? t("common.working") : ""}
        </p>
        {attempt.error !== null && (
          <p className="step-error" role="alert">
            {attempt.error}
          </p>
        )}
      </Form>
      {report !== null && <Report report={report} names={names} />}
    </Page>
  );
}
