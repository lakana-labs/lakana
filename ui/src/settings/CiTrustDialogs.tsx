/**
 * Add a CI trust rule and Edit a rule: the issuer, the environment and the repository (fixed
 * once the rule exists), the service account it acts as, what it allows, the claims the ID
 * token must have, and whether its deploys start the project's `on_deploy` jobs.
 *
 * @remarks
 * Saving asks for the passkey touch first (`guarded`); the server's refusal stays in the dialog
 * with every field kept. A managed environment shows the managed sentence in place of Add rule:
 * the interface never overrides an access file.
 * @packageDocumentation
 */
import { type JSX, useId, useState } from "react";
import {
  Button,
  CheckboxButton,
  CheckboxField,
  Form,
  Heading,
  Input,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { ManagedScopeBriefOut } from "../api/accessFile";
import {
  type CiTrustKind,
  type CiTrustRuleChanges,
  type CiTrustRuleOut,
  createCiTrustRule,
  updateCiTrustRule,
} from "../api/ciTrust";
import { type EnvironmentChoice, listEnvironments } from "../api/environments";
import { listRepositories } from "../api/repositories";
import "../access/accessFile.css";
import { Choice } from "../access/Choice";
import { FormFoot } from "../access/FormFoot";
import { GitMark } from "../access/GitMark";
import { managedScopeOf } from "../access/managed";
import { usePrincipalSearch } from "../access/suggest";
import { ActionDialog } from "../shell/ActionDialog";
import { EnvironmentName } from "../shell/EnvironmentName";
import { NamePicker } from "../shell/NamePicker";
import { useResource } from "../shell/useResource";
import { markedText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import "../access/access.css";
import "./ciTrust.css";
import "./ciTrustStrings";
import {
  type AddRuleStart,
  type ConditionRow,
  conditionsHelpKey,
  issuerPreset,
  type IssuerPreset,
  issuerWords,
  startingConditions,
} from "./ciTrustWords";

const P = "settings.ciTrust.dialog";

/** What the dialog does: add a rule, from an optional start, or edit one. */
export type RuleDialogMode =
  | { readonly kind: "add"; readonly start: AddRuleStart }
  | { readonly kind: "edit"; readonly rule: CiTrustRuleOut };

/** Properties of {@link RuleDialog}. */
export interface RuleDialogProps {
  /** Add or edit. */
  readonly mode: RuleDialogMode;
  /** The managed scopes. */
  readonly scopes: readonly ManagedScopeBriefOut[];
  /** Called with the rule once it is saved. */
  readonly onDone: (rule: CiTrustRuleOut) => void;
  /** Closes the dialog without saving. */
  readonly onClose: () => void;
}

/** Properties of {@link ConditionsField}. */
interface ConditionsFieldProps {
  /** The rows. */
  readonly rows: readonly ConditionRow[];
  /** Called with the rows after a change. */
  readonly onChange: (rows: readonly ConditionRow[]) => void;
  /** The sentence of which claims the issuer's rules must pin. */
  readonly help: string;
}

/**
 * The claim conditions, one row each: the claim's name and the text it must equal, with Remove,
 * and Add a claim under them. Each field is named by its row's number, since the column heads
 * are drawn once above the rows.
 *
 * @param props - The rows, the handler and the help.
 * @param props.rows - The rows.
 * @param props.onChange - Called with the rows after a change.
 * @param props.help - Which claims the rule must pin.
 * @returns The field.
 */
function ConditionsField({ rows, onChange, help }: ConditionsFieldProps): JSX.Element {
  const { t } = useTranslation();
  const change = (index: number, row: Partial<ConditionRow>): void => {
    onChange(rows.map((old, at) => (at === index ? { ...old, ...row } : old)));
  };
  return (
    <fieldset className="field ci-trust-conditions">
      <legend>{t(`${P}.conditions`)}</legend>
      <p className="page-purpose">{help}</p>
      <div className="ci-trust-condition ci-trust-condition-head" aria-hidden="true">
        <span>{t(`${P}.claimName`)}</span>
        <span>{t(`${P}.claimValue`)}</span>
      </div>
      {rows.map((row, index) => {
        const number = String(index + 1);
        return (
          // The rows have no id of their own; their place is what names their fields.
          <div className="ci-trust-condition" key={index}>
            <TextField
              className="field"
              aria-label={t(`${P}.claimNameOf`, { number })}
              value={row.name}
              onChange={(name) => {
                change(index, { name });
              }}
            >
              <Input autoComplete="off" spellCheck={false} />
            </TextField>
            <TextField
              className="field"
              aria-label={t(`${P}.claimValueOf`, { number })}
              value={row.value}
              onChange={(value) => {
                change(index, { value });
              }}
            >
              <Input autoComplete="off" spellCheck={false} />
            </TextField>
            <Button
              className="button"
              aria-label={
                row.name.trim() === ""
                  ? t(`${P}.removeClaimNumber`, { number })
                  : t(`${P}.removeClaimOf`, { name: row.name.trim() })
              }
              onPress={() => {
                onChange(rows.filter((_, at) => at !== index));
              }}
            >
              {t(`${P}.removeClaim`)}
            </Button>
          </div>
        );
      })}
      <p>
        <Button
          className="button"
          onPress={() => {
            onChange([...rows, { name: "", value: "" }]);
          }}
        >
          {t(`${P}.addClaim`)}
        </Button>
      </p>
    </fieldset>
  );
}

/** Properties of {@link KindsField}. */
interface KindsFieldProps {
  /** The kinds chosen. */
  readonly kinds: readonly CiTrustKind[];
  /** Called with the kinds after a change. */
  readonly onChange: (kinds: readonly CiTrustKind[]) => void;
}

/**
 * What the rule allows: deploy, dry run, or both, as two checkboxes.
 *
 * @param props - The kinds and the handler.
 * @param props.kinds - The kinds chosen.
 * @param props.onChange - Called after a change.
 * @returns The field.
 */
function KindsField({ kinds, onChange }: KindsFieldProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <fieldset className="field">
      <legend>{t(`${P}.kinds`)}</legend>
      {(["deploy", "dry_run"] as const).map((kind) => (
        <CheckboxField
          key={kind}
          isSelected={kinds.includes(kind)}
          onChange={(selected) => {
            onChange(
              selected
                ? [...kinds.filter((k) => k !== kind), kind]
                : kinds.filter((k) => k !== kind),
            );
          }}
        >
          <CheckboxButton className="checkbox">
            {t(`settings.ciTrust.kinds.${kind}`)}
          </CheckboxButton>
        </CheckboxField>
      ))}
    </fieldset>
  );
}

/** What the form holds. */
interface Fields {
  readonly issuer: IssuerPreset;
  readonly issuerUrl: string;
  readonly environment: string | null;
  readonly repository: string | null;
  readonly principal: string;
  readonly kinds: readonly CiTrustKind[];
  readonly conditions: readonly ConditionRow[];
  readonly startsJobs: boolean;
}

/**
 * The form's first state.
 *
 * @param mode - Add, from its start, or edit, from the rule.
 * @returns The fields.
 */
function firstFields(mode: RuleDialogMode): Fields {
  if (mode.kind === "edit") {
    const { rule } = mode;
    return {
      issuer: issuerPreset(rule.issuer),
      issuerUrl: rule.issuer,
      environment: rule.environment,
      repository: rule.repository,
      principal: rule.principal,
      kinds: rule.kinds,
      conditions: Object.entries(rule.conditions).map(([name, value]) => ({ name, value })),
      startsJobs: rule.start_on_deploy_jobs,
    };
  }
  const issuer = mode.start.issuer ?? "github";
  return {
    issuer,
    issuerUrl: "",
    environment: mode.start.environment,
    repository: mode.start.repository,
    principal: "",
    kinds: ["deploy"],
    conditions: startingConditions(issuer),
    startsJobs: false,
  };
}

/**
 * The conditions as the server takes them: names and texts as typed, a row with neither left
 * out.
 *
 * @param rows - The rows.
 * @returns The conditions.
 */
function conditionsOf(rows: readonly ConditionRow[]): Record<string, string> {
  return Object.fromEntries(
    rows
      .filter((row) => row.name !== "" || row.value !== "")
      .map((row) => [row.name.trim(), row.value]),
  );
}

/**
 * Whether two sets of conditions are the same.
 *
 * @param a - One set.
 * @param b - The other.
 * @returns The answer.
 */
function sameConditions(
  a: Readonly<Record<string, string>>,
  b: Readonly<Record<string, string>>,
): boolean {
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((key) => b[key] === a[key]);
}

/**
 * What an edit changes: only the fields that differ from the rule.
 *
 * @param rule - The rule.
 * @param fields - The form.
 * @returns The changes; empty when nothing changed.
 */
function changesOf(rule: CiTrustRuleOut, fields: Fields): CiTrustRuleChanges {
  const conditions = conditionsOf(fields.conditions);
  const kinds = [...fields.kinds].sort();
  return {
    ...(fields.principal.trim() === rule.principal ? {} : { principal: fields.principal.trim() }),
    ...(kinds.join() === [...rule.kinds].sort().join() ? {} : { kinds }),
    ...(sameConditions(conditions, rule.conditions) ? {} : { conditions }),
    ...(fields.startsJobs === rule.start_on_deploy_jobs
      ? {}
      : { start_on_deploy_jobs: fields.startsJobs }),
  };
}

/**
 * Says what the form still needs, in the order of its fields; null when it is complete.
 *
 * @param fields - The form.
 * @returns The key of the sentence, or null.
 */
function missingKey(fields: Fields): string | null {
  const needs: [boolean, string][] = [
    [fields.issuer === "other" && fields.issuerUrl.trim() === "", "issuerUrlNeeded"],
    [fields.environment === null || fields.environment.trim() === "", "environmentNeeded"],
    [fields.repository === null || fields.repository.trim() === "", "repositoryNeeded"],
    [fields.principal.trim() === "", "principalNeeded"],
    [fields.kinds.length === 0, "kindsNeeded"],
    [
      fields.conditions.length === 0 ||
        fields.conditions.some((row) => row.name.trim() === "" || row.value === ""),
      "conditionsNeeded",
    ],
  ];
  return needs.find(([lacking]) => lacking)?.[1] ?? null;
}

/**
 * Add a rule or edit one. Adding asks for the issuer, the environment and the repository
 * (offered from what the person may read; a plain field when nothing could be read), and
 * starts with the claims the server requires of the issuer's rules. Changing the issuer before
 * any claim is touched starts its own claims. Editing shows those three as facts and sends only
 * what changed. The submit button is held, saying why, until the form is complete (and for an
 * edit, until something changed).
 *
 * @param props - The mode, the managed scopes and the handlers.
 * @param props.mode - Add or edit.
 * @param props.scopes - The managed scopes.
 * @param props.onDone - Called once the rule is saved.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function RuleDialog({ mode, scopes, onDone, onClose }: RuleDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const startsHelp = useId();
  const suggest = usePrincipalSearch();
  const [fields, setFields] = useState<Fields>(() => firstFields(mode));
  const [touched, setTouched] = useState(mode.kind === "edit");
  const environments = useResource(listEnvironments);
  const repositories = useResource(listRepositories);
  const set = (change: Partial<Fields>): void => {
    setFields((previous) => ({ ...previous, ...change }));
  };
  const adding = mode.kind === "add";
  const managed =
    fields.environment === null ? null : managedScopeOf(scopes, fields.environment.trim());
  const lacking = missingKey(fields);
  const changes = mode.kind === "edit" ? changesOf(mode.rule, fields) : null;
  const held =
    lacking !== null
      ? t(`${P}.${lacking}`)
      : changes !== null && Object.keys(changes).length === 0
        ? t(`${P}.nothingChanged`)
        : null;
  const close = (): void => {
    if (!attempt.busy) onClose();
  };
  const save = async (): Promise<void> => {
    if (mode.kind === "edit") {
      onDone(await updateCiTrustRule(mode.rule.id, changesOf(mode.rule, fields)));
      return;
    }
    onDone(
      await createCiTrustRule({
        issuer: fields.issuer === "other" ? fields.issuerUrl.trim() : fields.issuer,
        principal: fields.principal.trim(),
        // Add rule is held until both are given.
        environment: (fields.environment as string).trim(),
        repository: (fields.repository as string).trim(),
        kinds: (["deploy", "dry_run"] as const).filter((kind) => fields.kinds.includes(kind)),
        conditions: conditionsOf(fields.conditions),
        start_on_deploy_jobs: fields.startsJobs,
      }),
    );
  };
  const environmentList = environments.data ?? [];
  const repositoryList = repositories.data ?? [];
  const rule = mode.kind === "edit" ? mode.rule : null;
  return (
    <ActionDialog onClose={close}>
      <Form
        className="ci-trust-form"
        onSubmit={(event) => {
          // The submit button is disabled while the form is held and absent in a managed
          // environment, so neither submits the form.
          event.preventDefault();
          attempt.run(save);
        }}
      >
        <Heading slot="title">
          {rule === null
            ? t(`${P}.addTitle`)
            : t(`${P}.editTitle`, { repository: rule.repository, environment: rule.environment })}
        </Heading>
        {rule === null ? (
          <>
            <RadioGroup
              className="field"
              orientation="horizontal"
              value={fields.issuer}
              onChange={(value) => {
                const issuer = value as IssuerPreset;
                set(touched ? { issuer } : { issuer, conditions: startingConditions(issuer) });
              }}
            >
              <Label>{t(`${P}.issuer`)}</Label>
              {(["github", "gitlab", "other"] as const).map((preset) => (
                <RadioField key={preset} value={preset} className="radio-field">
                  <RadioButton className="radio">
                    {t(`settings.ciTrust.issuers.${preset}`)}
                  </RadioButton>
                </RadioField>
              ))}
            </RadioGroup>
            {fields.issuer === "other" && (
              <TextField
                className="field"
                value={fields.issuerUrl}
                isRequired
                onChange={(issuerUrl) => {
                  set({ issuerUrl });
                }}
              >
                <Label>{t(`${P}.issuerUrl`)}</Label>
                <Input autoComplete="off" spellCheck={false} />
                <Text slot="description">{t(`${P}.issuerUrlHelp`)}</Text>
              </TextField>
            )}
            {environments.error !== null || environments.data?.length === 0 ? (
              <TextField
                className="field"
                value={fields.environment ?? ""}
                isRequired
                onChange={(environment) => {
                  set({ environment });
                }}
              >
                <Label>{t(`${P}.environment`)}</Label>
                <Input autoComplete="off" spellCheck={false} />
                <Text slot="description">{t(`${P}.environmentHelp`)}</Text>
              </TextField>
            ) : (
              <Choice<string>
                label={t(`${P}.environment`)}
                value={fields.environment}
                options={environmentList.map((choice) => ({
                  id: choice.name,
                  text: markedText(choice.name),
                }))}
                onChange={(environment) => {
                  set({ environment });
                }}
                render={(id) => (
                  <span>
                    <EnvironmentName
                      environment={
                        // Only listed environments are options.
                        environmentList.find((choice) => choice.name === id) as EnvironmentChoice
                      }
                    />
                    {managedScopeOf(scopes, id) !== null && (
                      <>
                        {" "}
                        <GitMark />
                      </>
                    )}
                  </span>
                )}
              />
            )}
            {repositories.error !== null || repositories.data?.length === 0 ? (
              <TextField
                className="field"
                value={fields.repository ?? ""}
                isRequired
                onChange={(repository) => {
                  set({ repository });
                }}
              >
                <Label>{t(`${P}.repository`)}</Label>
                <Input autoComplete="off" spellCheck={false} />
                <Text slot="description">{t(`${P}.repositoryHelp`)}</Text>
              </TextField>
            ) : (
              <Choice<string>
                label={t(`${P}.repository`)}
                value={fields.repository}
                options={repositoryList.map((repository) => ({
                  id: repository.name,
                  text: markedText(repository.name),
                }))}
                onChange={(repository) => {
                  set({ repository });
                }}
              />
            )}
          </>
        ) : (
          <dl className="fact-list">
            <dt>{t(`${P}.issuer`)}</dt>
            <dd>{markedText(issuerWords(t, rule.issuer))}</dd>
            <dt>{t(`${P}.environment`)}</dt>
            <dd>{markedText(rule.environment)}</dd>
            <dt>{t(`${P}.repository`)}</dt>
            <dd>{markedText(rule.repository)}</dd>
          </dl>
        )}
        <NamePicker
          label={t(`${P}.principal`)}
          help={t(`${P}.principalHelp`)}
          value={fields.principal}
          onChange={(principal) => {
            set({ principal });
          }}
          search={suggest}
          noMatch={(text) => t("picker.noPrincipal", { name: text })}
          isRequired
        />
        <KindsField
          kinds={fields.kinds}
          onChange={(kinds) => {
            set({ kinds });
          }}
        />
        <ConditionsField
          rows={fields.conditions}
          help={t(conditionsHelpKey(fields.issuer))}
          onChange={(conditions) => {
            setTouched(true);
            set({ conditions });
          }}
        />
        <div className="field">
          <CheckboxField
            isSelected={fields.startsJobs}
            aria-describedby={startsHelp}
            onChange={(startsJobs) => {
              set({ startsJobs });
            }}
          >
            <CheckboxButton className="checkbox">{t(`${P}.startsJobs`)}</CheckboxButton>
          </CheckboxField>
          <p className="page-purpose" id={startsHelp}>
            {t(`${P}.startsJobsHelp`)}
          </p>
        </div>
        {!adding && <p className="page-purpose">{t(`${P}.endsTokens`)}</p>}
        {managed !== null && (
          <p className="page-notice">
            <GitMark /> {t(`${P}.managed`, { environment: managed.scope, file: managed.file_name })}
          </p>
        )}
        <FormFoot
          attempt={attempt}
          submit={adding ? t(`${P}.create`) : t(`${P}.save`)}
          held={held}
          blocked={managed !== null}
          onCancel={close}
        />
      </Form>
    </ActionDialog>
  );
}
