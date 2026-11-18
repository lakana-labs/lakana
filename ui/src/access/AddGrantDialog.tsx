/**
 * Add grant: who, a role or a single action, and where; the form asks for the environment unless
 * the grant is on the whole instance.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useId, useRef, useState } from "react";
import {
  Button,
  Form,
  Heading,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  RadioButton,
  RadioField,
  RadioGroup,
  Select,
  SelectValue,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { ManagedScopeOut } from "../api/accessFile";
import type { EnvironmentChoice } from "../api/environments";
import {
  ACTIONS,
  type Action,
  createGrant,
  type GrantCreateIn,
  type GrantFilter,
  type GrantWhere,
  type Role,
  ROLES,
} from "../api/grants";
import { ActionDialog } from "../shell/ActionDialog";
import { EnvironmentName } from "../shell/EnvironmentPicker";
import { PendingButton } from "../shell/PendingButton";
import { markedText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import "./accessStrings";
import { GitMark } from "./GitMark";
import { managedScopeOf } from "./managed";
import { actionWords, roleName, roleSummary } from "./words";

/** The single actions that do nothing without the ADMIN role, which Add grant does not offer. */
export const NO_EFFECT_ACTIONS = ["MANAGE_GRANT", "MANAGE_GROUP", "MANAGE_TOKEN"] as const;

/** The actions Add grant offers. */
const OFFERED_ACTIONS = ACTIONS.filter(
  (action) => !(NO_EFFECT_ACTIONS as readonly string[]).includes(action),
);

/** Properties of {@link AddGrantDialog}. */
export interface AddGrantDialogProps {
  /** The list's filter, which the form starts from. */
  readonly start: GrantFilter;
  /** The environments to choose from. */
  readonly environments: readonly EnvironmentChoice[];
  /** The managed scopes. */
  readonly scopes: readonly ManagedScopeOut[];
  /** Called once the grant is made. */
  readonly onGranted: () => void;
  /** Closes the dialog without granting. */
  readonly onClose: () => void;
}

/** Properties of {@link Choice}. */
interface ChoiceProps<T extends string> {
  /** The label. */
  readonly label: string;
  /** The chosen key, or null. */
  readonly value: T | null;
  /** The options. */
  readonly options: readonly { readonly id: T; readonly text: string; readonly mark?: boolean }[];
  /** Called with the choice. */
  readonly onChange: (id: T) => void;
  /** Renders an option's content; its text by default. */
  readonly render?: (id: T) => JSX.Element;
}

/**
 * One labelled choice from a list, with "Choose…" until something is chosen.
 *
 * @template T - The keys.
 * @param props - The label, the choice, the options and the handler.
 * @param props.label - The label.
 * @param props.value - The chosen key.
 * @param props.options - The options.
 * @param props.onChange - Called with the choice.
 * @param props.render - Renders an option's content.
 * @returns The choice.
 */
function Choice<T extends string>({
  label,
  value,
  options,
  onChange,
  render,
}: ChoiceProps<T>): JSX.Element {
  const { t } = useTranslation();
  return (
    <Select
      className="env-picker access-choice"
      value={value}
      placeholder={t("access.addGrant.choose")}
      onChange={(key) => {
        // The only keys are the options'.
        onChange(String(key) as T);
      }}
    >
      <Label>{label}</Label>
      <Button className="button">
        <SelectValue />
        <span aria-hidden="true">▾</span>
      </Button>
      <Popover className="float" placement="bottom start">
        <ListBox className="menu">
          {options.map((option) => (
            <ListBoxItem
              key={option.id}
              id={option.id}
              className="menu-item"
              textValue={option.text}
            >
              <span className="menu-check" aria-hidden="true" />
              {render === undefined ? option.text : render(option.id)}
            </ListBoxItem>
          ))}
        </ListBox>
      </Popover>
    </Select>
  );
}

/**
 * The form's fields at the start: the list's filter, else a role on an environment.
 *
 * @param start - The list's filter.
 * @returns The fields.
 */
function startFrom(start: GrantFilter): {
  who: "principal" | "group";
  name: string;
  what: "role" | "action";
  role: Role | null;
  action: Action | null;
  where: GrantWhere["kind"];
  environment: string | null;
  namespace: string;
  table: string;
} {
  const { who, what, where } = start;
  return {
    who: who?.kind ?? "principal",
    name: who?.name ?? "",
    what: what?.kind ?? "role",
    role: what?.kind === "role" ? what.role : null,
    action:
      what?.kind === "action" && !(NO_EFFECT_ACTIONS as readonly string[]).includes(what.action)
        ? what.action
        : null,
    where: where?.kind ?? "ENVIRONMENT",
    environment: where === null || where.kind === "INSTANCE" ? null : where.environment,
    namespace: where?.kind === "NAMESPACE" || where?.kind === "TABLE" ? where.namespace : "",
    table: where?.kind === "TABLE" ? where.table : "",
  };
}

/**
 * Renders the form. Grant is held, and says why, until the form names who, what and every
 * part of where; a managed scope shows the managed sentence in place of Grant. Grant asks for
 * the passkey touch first; the server's refusal shows under the form and takes the focus, with
 * every field kept. While the request is open, Escape and Cancel do nothing.
 *
 * @param props - Where the form starts, the environments, the managed scopes and the handlers.
 * @param props.start - The list's filter.
 * @param props.environments - The environments to choose from.
 * @param props.scopes - The managed scopes.
 * @param props.onGranted - Called once the grant is made.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function AddGrantDialog({
  start,
  environments,
  scopes,
  onGranted,
  onClose,
}: AddGrantDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [fields, setFields] = useState(() => startFrom(start));
  const set = (change: Partial<typeof fields>): void => {
    setFields((previous) => ({ ...previous, ...change }));
  };
  const heldId = useId();
  const alert = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (attempt.error !== null) alert.current?.focus();
  }, [attempt.error]);
  const { who, name, what, role, action, where, environment, namespace, table } = fields;
  const onInstance = where === "INSTANCE";
  const managed = managedScopeOf(scopes, onInstance ? null : environment);
  let held: string | null = null;
  if (name.trim() === "") held = t("access.addGrant.nameNeeded");
  else if ((what === "role" ? role : action) === null) held = t("access.addGrant.whatNeeded");
  else if (!onInstance && environment === null) held = t("access.addGrant.environmentNeeded");
  else if ((where === "NAMESPACE" || where === "TABLE") && namespace.trim() === "") {
    held = t("access.addGrant.namespaceNeeded");
  } else if (where === "TABLE" && table.trim() === "") held = t("access.addGrant.tableNeeded");
  const body = (): GrantCreateIn => ({
    ...(who === "principal" ? { principal: name.trim() } : { group: name.trim() }),
    ...(what === "role" ? { role: role as Role } : { action: action as Action }),
    kind: where,
    ...(onInstance ? {} : { environment: environment as string }),
    ...(where === "NAMESPACE" || where === "TABLE" ? { namespace: namespace.trim() } : {}),
    ...(where === "TABLE" ? { table: table.trim() } : {}),
  });
  const close = (): void => {
    if (!attempt.busy) onClose();
  };
  const managedEnvironments = new Set(
    environments
      .map((choice) => choice.name)
      .filter((choice) => managedScopeOf(scopes, choice) !== null),
  );
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (held !== null || managed !== null || attempt.busy) return;
          attempt.run(async () => {
            await createGrant(body());
            onGranted();
          });
        }}
      >
        <Heading slot="title">{t("access.addGrant.title")}</Heading>
        <RadioGroup
          className="field"
          orientation="horizontal"
          value={who}
          onChange={(value) => {
            set({ who: value as typeof who });
          }}
        >
          <Label>{t("access.addGrant.who")}</Label>
          <RadioField value="principal" className="radio-field">
            <RadioButton className="radio">{t("access.addGrant.person")}</RadioButton>
          </RadioField>
          <RadioField value="group" className="radio-field">
            <RadioButton className="radio">{t("access.addGrant.group")}</RadioButton>
          </RadioField>
        </RadioGroup>
        <TextField
          className="field"
          value={name}
          onChange={(value) => {
            set({ name: value });
          }}
        >
          <Label>{t("access.addGrant.name")}</Label>
          <Input autoComplete="off" spellCheck={false} />
        </TextField>
        <RadioGroup
          className="field"
          orientation="horizontal"
          value={what}
          onChange={(value) => {
            set({ what: value as typeof what });
          }}
        >
          <Label>{t("access.addGrant.what")}</Label>
          <RadioField value="role" className="radio-field">
            <RadioButton className="radio">{t("access.addGrant.role")}</RadioButton>
          </RadioField>
          <RadioField value="action" className="radio-field">
            <RadioButton className="radio">{t("access.addGrant.action")}</RadioButton>
          </RadioField>
        </RadioGroup>
        {what === "role" ? (
          <Choice<Role>
            label={t("access.addGrant.roleLabel")}
            value={role}
            options={ROLES.map((id) => ({ id, text: roleName(t, id) }))}
            onChange={(id) => {
              set({ role: id });
            }}
            render={(id) => (
              <span>
                {roleName(t, id)}
                <span className="second">{roleSummary(t, id)}</span>
              </span>
            )}
          />
        ) : (
          <Choice<Action>
            label={t("access.addGrant.actionLabel")}
            value={action}
            options={OFFERED_ACTIONS.map((id) => ({ id, text: actionWords(t, id) }))}
            onChange={(id) => {
              set({ action: id });
            }}
          />
        )}
        <RadioGroup
          className="field"
          value={where}
          onChange={(value) => {
            set({ where: value as typeof where });
          }}
        >
          <Label>{t("access.addGrant.where")}</Label>
          <RadioField value="INSTANCE" className="radio-field">
            <RadioButton className="radio">{t("access.grants.filters.instance")}</RadioButton>
          </RadioField>
          <RadioField value="ENVIRONMENT" className="radio-field">
            <RadioButton className="radio">{t("access.grants.filters.environment")}</RadioButton>
          </RadioField>
          <RadioField value="NAMESPACE" className="radio-field">
            <RadioButton className="radio">{t("access.grants.filters.namespace")}</RadioButton>
          </RadioField>
          <RadioField value="TABLE" className="radio-field">
            <RadioButton className="radio">{t("access.grants.filters.table")}</RadioButton>
          </RadioField>
        </RadioGroup>
        {!onInstance && (
          <Choice<string>
            label={t("access.grants.filters.environmentPicker")}
            value={environment}
            options={environments.map((choice) => ({
              id: choice.name,
              text: markedText(choice.name),
            }))}
            onChange={(id) => {
              set({ environment: id });
            }}
            render={(id) => (
              <span>
                <EnvironmentName
                  environment={
                    environments.find((choice) => choice.name === id) as EnvironmentChoice
                  }
                />
                {managedEnvironments.has(id) && (
                  <>
                    {" "}
                    <GitMark />
                  </>
                )}
              </span>
            )}
          />
        )}
        {(where === "NAMESPACE" || where === "TABLE") && (
          <TextField
            className="field"
            value={namespace}
            onChange={(value) => {
              set({ namespace: value });
            }}
          >
            <Label>{t("access.grants.filters.namespaceName")}</Label>
            <Input autoComplete="off" spellCheck={false} />
          </TextField>
        )}
        {where === "TABLE" && (
          <TextField
            className="field"
            value={table}
            onChange={(value) => {
              set({ table: value });
            }}
          >
            <Label>{t("access.grants.filters.tableName")}</Label>
            <Input autoComplete="off" spellCheck={false} />
          </TextField>
        )}
        {managed !== null ? (
          <p className="page-notice">
            <GitMark />{" "}
            {/* An access file that manages the instance hides Add grant: this is an environment. */}
            {t("access.managed.addGrant", { environment: managed.scope, file: managed.file_name })}
          </p>
        ) : null}
        <div className="step-actions">
          {managed === null && (
            <PendingButton
              type="submit"
              className="button button-primary"
              pending={attempt.busy}
              isDisabled={held !== null}
              {...(held === null ? {} : { "aria-describedby": heldId })}
            >
              {t("access.addGrant.grant")}
            </PendingButton>
          )}
          <Button className="button" onPress={close}>
            {t("dialog.cancel")}
          </Button>
        </div>
        {managed === null && held !== null && (
          <p className="step-status" id={heldId}>
            {held}
          </p>
        )}
        <p className="step-status" role="status">
          {attempt.busy ? t("common.working") : ""}
        </p>
        {attempt.error !== null && (
          <p className="step-error" role="alert" tabIndex={-1} ref={alert}>
            {attempt.error}
            {attempt.addPasskey && (
              <>
                {" "}
                <Link to="/account">{t("passkey.add")}</Link>
              </>
            )}
          </p>
        )}
      </Form>
    </ActionDialog>
  );
}
