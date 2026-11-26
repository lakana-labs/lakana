/**
 * Settings › CI trust: the rules that let CI jobs deploy with their OIDC ID token, each
 * opening its page, Add rule, and the way to Explain a CI job's claims.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, type RefObject, Suspense, useEffect, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useSearchParams } from "react-router";

import type { ManagedScopeBriefOut } from "../api/accessFile";
import { type CiTrustRuleOut, listCiTrustRules } from "../api/ciTrust";
import { ManagedMark } from "../access/ListBits";
import { managedScopeOf } from "../access/managed";
import { ManagedNote } from "../access/ManagedNote";
import { useAccessStatus } from "../access/useAccessStatus";
import { ReadFailed } from "../catalog/ReadFailed";
import { ViewList } from "../catalog/views/ViewList";
import { useFocusAfter } from "../shell/useFocusAfter";
import { type Resource, useResource } from "../shell/useResource";
import { VisibleText } from "../shell/VisibleText";
import "./ciTrust.css";
import "./ciTrustStrings";
import {
  type AddRuleStart,
  ciTrustRulePath,
  EXPLAIN_PATH,
  issuerWords,
  kindsWords,
  readAddRule,
} from "./ciTrustWords";
import type { LeftRuleState } from "./CiTrustRulePage";
import { SettingsFrame } from "./SettingsFrame";
import "./settingsStrings";
import { useWholePages } from "./wholePages";

// The dialog loads when it is first opened, so the list stays small.
const RuleDialog = lazy(async () => ({ default: (await import("./CiTrustDialogs")).RuleDialog }));

const P = "settings.ciTrust";

/** Properties of {@link RuleList}. */
interface RuleListProps {
  /** The rules' read. */
  readonly read: Resource<readonly CiTrustRuleOut[]>;
  /** The managed scopes; empty until read. */
  readonly scopes: readonly ManagedScopeBriefOut[];
  /** The place's heading, focused after Try again and Refresh. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
}

/**
 * The rules in their own scrolling region, oldest first as the server lists them: what each
 * lets deploy where, its issuer, the account it acts as, what it allows and its state.
 *
 * @param props - The read, the managed scopes and the heading.
 * @param props.read - The rules' read.
 * @param props.scopes - The managed scopes.
 * @param props.heading - The place's heading.
 * @returns The list.
 */
function RuleList({ read, scopes, heading }: RuleListProps): JSX.Element {
  const { t } = useTranslation();
  const pages = useWholePages(read, heading);
  const rows = read.data ?? [];
  return (
    <ViewList
      pages={pages}
      rowCount={rows.length}
      empty={t(`${P}.none`)}
      region={t(`${P}.region`)}
      rowsOpen
      scroll="ci-trust-scroll"
      toolbar={{ count: P }}
      head={
        <tr>
          <th scope="col">{t(`${P}.rule`)}</th>
          <th scope="col">{t(`${P}.issuer`)}</th>
          <th scope="col">{t(`${P}.actsAs`)}</th>
          <th scope="col">{t(`${P}.allows`)}</th>
          <th scope="col">{t(`${P}.state`)}</th>
          <th scope="col">
            <span className="visually-hidden">{t("list.open")}</span>
          </th>
        </tr>
      }
    >
      {rows.map((rule) => {
        const managed = managedScopeOf(scopes, rule.environment);
        return (
          <tr key={rule.id}>
            <td>
              <Link className="row-name" to={ciTrustRulePath(rule.id)}>
                {t(`${P}.ruleName`, { repository: rule.repository, environment: rule.environment })}
              </Link>
            </td>
            <td className="ci-trust-issuer">
              <VisibleText text={issuerWords(t, rule.issuer)} />
            </td>
            <td>
              <VisibleText text={rule.principal} />
            </td>
            <td>{kindsWords(t, rule.kinds)}</td>
            <td>
              <span className={`pill ${rule.enabled ? "pill-neutral" : "pill-warn"}`}>
                {rule.enabled ? t(`${P}.enabled`) : t(`${P}.disabled`)}
              </span>
              {managed !== null && (
                <>
                  {" "}
                  <ManagedMark file={managed.file_name} />
                </>
              )}
            </td>
            <td>
              <span className="chevron" aria-hidden="true">
                ›
              </span>
            </td>
          </tr>
        );
      })}
    </ViewList>
  );
}

/**
 * Renders the purpose, the list and Add rule. Until the access files' status is read, and
 * whenever an access file manages the instance, nothing can be added: the note says why. An
 * address with `?add=1` (the end of Connect a repository links to one) opens Add rule filled in
 * from it, once: the address then drops the question, so going back does not open it again.
 *
 * @returns The place.
 */
export function CiTrustPlace(): JSX.Element {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState<AddRuleStart | false>(() => readAddRule(params) ?? false);
  const asked = params.has("add");
  useEffect(() => {
    if (asked) setParams({}, { replace: true });
  }, [asked, setParams]);
  const left = (useLocation().state as Partial<LeftRuleState> | null)?.note;
  const [note, setNote] = useState(left ?? "");
  const read = useResource(listCiTrustRules);
  const status = useAccessStatus();
  const { target: heading, arm } = useFocusAfter<HTMLHeadingElement>(read.data);
  const scopes = status.data?.scopes ?? [];
  const managedInstance = managedScopeOf(scopes, null);
  const writable = status.data !== undefined && managedInstance === null;
  return (
    <SettingsFrame
      title={t("settings.places.ciTrust")}
      headingRef={heading}
      action={
        writable && (
          <Button
            className="button button-primary"
            onPress={() => {
              setAdding({ repository: null, environment: null, issuer: null });
            }}
          >
            {t(`${P}.add`)}
          </Button>
        )
      }
    >
      <p className="page-purpose">{t(`${P}.intro`)}</p>
      <p className="page-purpose">
        {t(`${P}.explainIntro`)} <Link to={EXPLAIN_PATH}>{t(`${P}.explainLink`)}</Link>
      </p>
      {status.error !== null && (
        <ReadFailed
          message={t("access.managed.readFailed", { sentence: status.error })}
          onRetry={() => {
            heading.current?.focus();
            status.reload();
          }}
        />
      )}
      {managedInstance !== null && (
        <ManagedNote scope={managedInstance} commands={[t(`${P}.managedCommand`)]} />
      )}
      <p className="step-status" role="status">
        {note}
      </p>
      <RuleList read={read} scopes={scopes} heading={heading} />
      {adding !== false && writable && (
        <Suspense fallback={null}>
          <RuleDialog
            mode={{ kind: "add", start: adding }}
            scopes={scopes}
            onClose={() => {
              setAdding(false);
            }}
            onDone={(rule) => {
              setAdding(false);
              setNote(
                t(`${P}.added`, { repository: rule.repository, environment: rule.environment }),
              );
              arm();
              read.reload();
            }}
          />
        </Suspense>
      )}
    </SettingsFrame>
  );
}
