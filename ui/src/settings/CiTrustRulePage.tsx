/**
 * A rule's page in Settings › CI trust: what it lets deploy where and as whom, the claims the
 * ID token must have, the way to the workflow, and for an unmanaged environment Edit, Enable or
 * Disable, and Delete rule.
 *
 * @remarks
 * Every change asks for the passkey touch first, as the server does; the server's refusal
 * stays where the change was asked for. The page is read again after each change.
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useCallback, useEffect, useId, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import {
  type CiTrustRuleOut,
  deleteCiTrustRule,
  getCiTrustRule,
  setCiTrustRuleEnabled,
} from "../api/ciTrust";
import { grantsPath } from "../access/accessPaths";
import { managedScopeOf } from "../access/managed";
import { ManagedNote } from "../access/ManagedNote";
import { personPath } from "../access/personPath";
import { useAccessStatus } from "../access/useAccessStatus";
import { ReadFailed } from "../catalog/ReadFailed";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { HOW_TO_URL } from "../shell/docs";
import { Page } from "../shell/Page";
import { PendingButton } from "../shell/PendingButton";
import { useFocusAfter } from "../shell/useFocusAfter";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import "./ciTrust.css";
import "./ciTrustStrings";
import { CI_TRUST_PATH, issuerPreset, issuerWords, kindsWords } from "./ciTrustWords";
import { DetailRead } from "./DetailRead";
import { environmentPath } from "./environmentPaths";
import { repositoryPath } from "./repositoryPaths";
import { type ListSortSpec, sortRows } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import "./settingsStrings";

const RuleDialog = lazy(async () => ({ default: (await import("./CiTrustDialogs")).RuleDialog }));

const P = "settings.ciTrust.page";

/** What a rule's page tells the list it goes back to. */
export interface LeftRuleState {
  /** The sentence the list says once it is shown. */
  readonly note: string;
}

/** Properties of {@link RuleFacts}. */
interface RuleFactsProps {
  /** The rule. */
  readonly rule: CiTrustRuleOut;
}

/**
 * The rule's facts: its state, issuer, environment, repository, account, what it allows, what a
 * deploy starts, who made and changed it, and its id; and that its account needs DEPLOY.
 *
 * @param props - The rule.
 * @param props.rule - The rule.
 * @returns The facts.
 */
function RuleFacts({ rule }: RuleFactsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  return (
    <>
      <dl className="fact-list">
        <dt>{t("settings.ciTrust.state")}</dt>
        <dd>
          <span className={`pill ${rule.enabled ? "pill-neutral" : "pill-warn"}`}>
            {rule.enabled ? t("settings.ciTrust.enabled") : t("settings.ciTrust.disabled")}
          </span>
        </dd>
        <dt>{t("settings.ciTrust.issuer")}</dt>
        <dd className="ci-trust-issuer">
          <VisibleText text={issuerWords(t, rule.issuer)} />
        </dd>
        <dt>{t(`${P}.environment`)}</dt>
        <dd>
          <Link to={environmentPath(rule.environment)}>
            <VisibleText text={rule.environment} />
          </Link>
        </dd>
        <dt>{t(`${P}.repository`)}</dt>
        <dd>
          <Link to={repositoryPath(rule.repository)}>
            <VisibleText text={rule.repository} />
          </Link>
        </dd>
        <dt>{t("settings.ciTrust.actsAs")}</dt>
        <dd>
          <Link to={personPath(rule.principal)}>
            <VisibleText text={rule.principal} />
          </Link>
        </dd>
        <dt>{t("settings.ciTrust.allows")}</dt>
        <dd>{kindsWords(t, rule.kinds)}</dd>
        <dt>{t(`${P}.startsJobs`)}</dt>
        <dd>
          {rule.start_on_deploy_jobs
            ? t(`${P}.startsJobsYes`, { environment: rule.environment })
            : t(`${P}.startsJobsNo`)}
        </dd>
        <dt>{t(`${P}.created`)}</dt>
        <dd>
          {rule.created_by === null
            ? t(`${P}.createdByGone`, { when: when(rule.created_at) })
            : t(`${P}.createdBy`, { when: when(rule.created_at), who: rule.created_by })}
        </dd>
        <dt>{t(`${P}.changed`)}</dt>
        <dd>
          {rule.updated_by === null
            ? t(`${P}.neverChanged`)
            : t(`${P}.createdBy`, { when: when(rule.updated_at), who: rule.updated_by })}
        </dd>
        <dt>{t(`${P}.id`)}</dt>
        <dd>
          <code>{rule.id}</code>
        </dd>
      </dl>
      <p className="page-purpose">
        {t(`${P}.deployGrant`, { principal: rule.principal, environment: rule.environment })}{" "}
        <Link to={grantsPath({ who: { kind: "principal", name: rule.principal } })}>
          {t(`${P}.checkGrants`, { principal: rule.principal })}
        </Link>
      </p>
    </>
  );
}

/** Properties of {@link RuleConditions}. */
interface RuleConditionsProps {
  /** The rule. */
  readonly rule: CiTrustRuleOut;
}

/** The columns a rule's claims sort by, in the browser. */
const CONDITION_SORT: ListSortSpec<"claim" | "value"> = {
  fields: { claim: "asc", value: "asc" },
  natural: null,
};

/**
 * The claims the ID token must have, each with the exact text, in their own scrolling region.
 *
 * @param props - The rule.
 * @param props.rule - The rule.
 * @returns The panel.
 */
function RuleConditions({ rule }: RuleConditionsProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  const sorting = useListSort(CONDITION_SORT);
  return (
    <section className="flat-panel settings-panel ci-trust-links" aria-labelledby={id}>
      <h2 id={id}>{t(`${P}.conditions`)}</h2>
      <p className="page-purpose">{t(`${P}.conditionsHelp`)}</p>
      <div
        className="list-scroll ci-trust-conditions-scroll"
        role="region"
        aria-label={t(`${P}.claimsRegion`)}
        // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
      >
        <table className="list">
          <thead>
            <tr>
              <SortHeader {...sorting.header("claim")}>{t(`${P}.claim`)}</SortHeader>
              <SortHeader {...sorting.header("value")}>{t(`${P}.value`)}</SortHeader>
            </tr>
          </thead>
          <tbody>
            {sortRows(Object.entries(rule.conditions), sorting.sort, {
              claim: ([name]) => name,
              value: ([, value]) => value,
            }).map(([name, value]) => (
              <tr key={name}>
                <td>
                  <code>
                    <VisibleText text={name} />
                  </code>
                </td>
                <td>
                  <code>
                    <VisibleText text={value} />
                  </code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** Properties of {@link Workflow}. */
interface WorkflowProps {
  /** The rule. */
  readonly rule: CiTrustRuleOut;
}

/**
 * Where the workflow that uses the rule is: the how-to of its CI provider, which has the file
 * `lakana init` writes.
 *
 * @param props - The rule.
 * @param props.rule - The rule.
 * @returns The panel.
 */
function Workflow({ rule }: WorkflowProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  const github = issuerPreset(rule.issuer) === "github";
  return (
    <section className="flat-panel settings-panel ci-trust-links" aria-labelledby={id}>
      <h2 id={id}>{t(`${P}.workflow`)}</h2>
      <p>{t(`${P}.workflowText`)}</p>
      <p>
        <a href={`${HOW_TO_URL}deploy-from-${github ? "github-actions" : "gitlab-ci"}/`}>
          {github ? t(`${P}.workflowGithub`) : t(`${P}.workflowGitlab`)}
        </a>
      </p>
    </section>
  );
}

/** Properties of {@link CiTrustRulePage}. */
export interface CiTrustRulePageProps {
  /** The rule's id, from the address. */
  readonly id: string;
}

/** Which dialog is open. */
type Open = "edit" | "disable" | "delete";

/**
 * Renders the rule's page. An unknown rule shows the server's sentence and the way back. While
 * the access files' status is unread, or an access file manages the rule's environment (or the
 * instance), nothing can be changed: the note names the file and the command for an emergency.
 *
 * @param props - The rule.
 * @param props.id - The rule's id.
 * @returns The page.
 */
export function CiTrustRulePage({ id }: CiTrustRulePageProps): JSX.Element {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const read = useResource(useCallback(() => getCiTrustRule(id), [id]));
  const factsId = useId();
  const status = useAccessStatus();
  const heading = useRef<HTMLHeadingElement>(null);
  const { target: stateButton, arm: armState } = useFocusAfter<HTMLButtonElement>(read.data);
  const { target: editButton, arm: armEdit } = useFocusAfter<HTMLButtonElement>(read.data);
  const enabling = useAttempt();
  const [open, setOpen] = useState<Open | null>(null);
  const [note, setNote] = useState("");
  const [left, setLeft] = useState<string | null>(null);
  // The dialog closes first, in its own commit, so its return of the focus is over before the
  // list takes the place of this page and the shell focuses that page's heading.
  useEffect(() => {
    if (left !== null) {
      void navigate(CI_TRUST_PATH, {
        replace: true,
        state: { note: left } satisfies LeftRuleState,
      });
    }
  }, [left, navigate]);
  const rule = read.data;
  const title =
    rule === undefined
      ? t("settings.places.ciTrust")
      : t("settings.ciTrust.ruleName", {
          repository: rule.repository,
          environment: rule.environment,
        });
  const managed =
    status.data === undefined || rule === undefined
      ? undefined
      : managedScopeOf(status.data.scopes, rule.environment);
  const writable = managed === null;
  const scopes = status.data?.scopes ?? [];
  const close = (): void => {
    setOpen(null);
  };
  return (
    <Page
      title={title}
      headingRef={heading}
      trail={[
        { to: "/settings", label: t("areas.settings.name") },
        { to: CI_TRUST_PATH, label: t("settings.places.ciTrust") },
      ]}
    >
      <DetailRead
        read={read}
        heading={heading}
        missing={<Link to={CI_TRUST_PATH}>{t(`${P}.back`)}</Link>}
      >
        {(shown) => {
          const names = { repository: shown.repository, environment: shown.environment };
          return (
            <>
              {status.error !== null && (
                <ReadFailed
                  message={t("access.managed.readFailed", { sentence: status.error })}
                  onRetry={() => {
                    heading.current?.focus();
                    status.reload();
                  }}
                />
              )}
              {managed !== undefined && managed !== null && (
                <ManagedNote
                  scope={managed}
                  commands={[t("settings.ciTrust.managedRuleCommand", { id: shown.id })]}
                />
              )}
              <p className="step-status" role="status">
                {note}
              </p>
              <section
                className="flat-panel settings-panel ci-trust-links"
                aria-labelledby={factsId}
              >
                <div className="panel-head">
                  <h2 className="panel-title" id={factsId}>
                    {t(`${P}.facts`)}
                  </h2>
                  {writable && (
                    <div className="button-row">
                      <Button
                        ref={editButton}
                        className="button"
                        aria-label={t(`${P}.editOf`, names)}
                        onPress={() => {
                          setOpen("edit");
                        }}
                      >
                        {t(`${P}.edit`)}
                      </Button>
                      {shown.enabled ? (
                        <Button
                          ref={stateButton}
                          className="button"
                          aria-label={t(`${P}.disableOf`, names)}
                          onPress={() => {
                            setOpen("disable");
                          }}
                        >
                          {t(`${P}.disable`)}
                        </Button>
                      ) : (
                        <PendingButton
                          ref={stateButton}
                          className="button"
                          aria-label={t(`${P}.enableOf`, names)}
                          pending={enabling.busy}
                          onPress={() => {
                            enabling.run(async () => {
                              await setCiTrustRuleEnabled(shown.id, true);
                              setNote(t(`${P}.enabledNote`));
                              armState();
                              read.reload();
                            });
                          }}
                        >
                          {t(`${P}.enable`)}
                        </PendingButton>
                      )}
                    </div>
                  )}
                </div>
                <AttemptStatus attempt={enabling} waiting={t("common.working")} />
                <RuleFacts rule={shown} />
              </section>
              <RuleConditions rule={shown} />
              <Workflow rule={shown} />
              {writable && (
                <p>
                  <Button
                    className="button"
                    aria-label={t(`${P}.deleteOf`, names)}
                    onPress={() => {
                      setOpen("delete");
                    }}
                  >
                    {t(`${P}.delete`)}
                  </Button>
                </p>
              )}
              {open === "edit" && (
                <Suspense fallback={null}>
                  <RuleDialog
                    mode={{ kind: "edit", rule: shown }}
                    scopes={scopes}
                    onClose={close}
                    onDone={() => {
                      close();
                      setNote(t(`${P}.savedNote`));
                      armEdit();
                      read.reload();
                    }}
                  />
                </Suspense>
              )}
              {open === "disable" && (
                <ConfirmDialog
                  title={t(`${P}.disableTitle`, names)}
                  confirmLabel={t(`${P}.disable`)}
                  onConfirm={async () => {
                    await setCiTrustRuleEnabled(shown.id, false);
                  }}
                  onCancel={close}
                  onDone={() => {
                    close();
                    setNote(t(`${P}.disabledNote`));
                    armState();
                    read.reload();
                  }}
                >
                  <p>{t(`${P}.disableText`)}</p>
                </ConfirmDialog>
              )}
              {open === "delete" && (
                <ConfirmDialog
                  title={t(`${P}.deleteTitle`, names)}
                  confirmLabel={t(`${P}.deleteConfirm`)}
                  onConfirm={() => deleteCiTrustRule(shown.id)}
                  onCancel={close}
                  onDone={() => {
                    close();
                    setLeft(t(`${P}.deleted`, names));
                  }}
                >
                  <p>{t(`${P}.deleteText`)}</p>
                </ConfirmDialog>
              )}
            </>
          );
        }}
      </DetailRead>
    </Page>
  );
}
