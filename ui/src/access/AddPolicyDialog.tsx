/**
 * Add filter or mask: the kind, who it is for, the table (and a mask's column) and the
 * expression.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Form,
  Heading,
  Input,
  Label,
  RadioButton,
  RadioField,
  RadioGroup,
  Text,
  TextArea,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { ManagedScopeBriefOut } from "../api/accessFile";
import type { EnvironmentChoice } from "../api/environments";
import {
  createColumnMask,
  createRowFilter,
  type PolicyFilter,
  type PolicyKind,
  type RowFilterCreateIn,
} from "../api/policies";
import { ActionDialog } from "../shell/ActionDialog";
import { EnvironmentName } from "../shell/EnvironmentPicker";
import { markedText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import "./accessStrings";
import { Choice } from "./Choice";
import { FormFoot } from "./FormFoot";
import { GitMark } from "./GitMark";
import { managedScopeOf } from "./managed";
import "./policies.css";
import "./policyStrings";

/** Properties of {@link AddPolicyDialog}. */
export interface AddPolicyDialogProps {
  /** The lists' filter, which the form starts from. */
  readonly start: PolicyFilter;
  /** The environments to choose from; null until read, none when the person may list none. */
  readonly environments: readonly EnvironmentChoice[] | null;
  /** The managed scopes. */
  readonly scopes: readonly ManagedScopeBriefOut[];
  /** Called with the kind once the policy is made. */
  readonly onCreated: (kind: PolicyKind) => void;
  /** Closes the dialog without creating. */
  readonly onClose: () => void;
}

/** What the form holds. */
interface Fields {
  /** A row filter or a column mask. */
  readonly kind: PolicyKind;
  /** For a person or service account, or for a group. */
  readonly who: "principal" | "group";
  /** Their name. */
  readonly name: string;
  /** The table's environment. */
  readonly environment: string | null;
  /** The table's dotted namespace. */
  readonly namespace: string;
  /** The table. */
  readonly table: string;
  /** A mask's column. */
  readonly column: string;
  /** The SQL expression. */
  readonly expression: string;
}

/**
 * The form's fields at the start: the lists' filter, else a row filter for a person.
 *
 * @param start - The lists' filter.
 * @returns The fields.
 */
function startFrom(start: PolicyFilter): Fields {
  return {
    kind: "row-filter",
    who: start.who?.kind ?? "principal",
    name: start.who?.name ?? "",
    environment: start.environment,
    namespace: start.namespace ?? "",
    table: start.table ?? "",
    column: "",
    expression: "",
  };
}

/**
 * Says what the form still needs, in the order of its fields; null when it is complete.
 *
 * @param t - The translator.
 * @param fields - The form.
 * @returns The sentence, or null.
 */
function missing(t: ReturnType<typeof useTranslation>["t"], fields: Fields): string | null {
  const needs: [boolean, string][] = [
    [fields.name.trim() === "", "nameNeeded"],
    [fields.environment === null, "environmentNeeded"],
    [fields.namespace.trim() === "", "namespaceNeeded"],
    [fields.table.trim() === "", "tableNeeded"],
    [fields.kind === "column-mask" && fields.column.trim() === "", "columnNeeded"],
    [fields.expression.trim() === "", "expressionNeeded"],
  ];
  const need = needs.find(([lacking]) => lacking);
  return need === undefined ? null : t(`policies.addDialog.${need[1]}`);
}

/** Properties of {@link PlainField}. */
interface PlainFieldProps {
  /** The label. */
  readonly label: string;
  /** What is typed. */
  readonly value: string;
  /** Called as the person types. */
  readonly onChange: (value: string) => void;
  /** A help line under the field. */
  readonly help?: string;
}

/**
 * A required text field of the form.
 *
 * @param props - The label, the value, the handler and the help.
 * @param props.label - The label.
 * @param props.value - What is typed.
 * @param props.onChange - Called as the person types.
 * @param props.help - A help line.
 * @returns The field.
 */
function PlainField({ label, value, onChange, help }: PlainFieldProps): JSX.Element {
  return (
    <TextField className="field" value={value} onChange={onChange} isRequired>
      <Label>{label}</Label>
      <Input autoComplete="off" spellCheck={false} />
      {help !== undefined && <Text slot="description">{help}</Text>}
    </TextField>
  );
}

/**
 * Renders the form. Create is held, and says why, until every field is filled; a managed
 * environment shows the managed sentence in place of Create. Create asks for the passkey touch
 * first; the server's refusal (an unknown name, table or column, an expression it cannot take)
 * shows under the form and takes the focus, with every field kept. While the request is open,
 * Escape and Cancel do nothing. Nothing checks the expression before the server does.
 *
 * @param props - Where the form starts, the environments, the managed scopes and the handlers.
 * @param props.start - The lists' filter.
 * @param props.environments - The environments to choose from.
 * @param props.scopes - The managed scopes.
 * @param props.onCreated - Called with the kind once the policy is made.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function AddPolicyDialog({
  start,
  environments,
  scopes,
  onCreated,
  onClose,
}: AddPolicyDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [fields, setFields] = useState(() => startFrom(start));
  const set = (change: Partial<Fields>): void => {
    setFields((previous) => ({ ...previous, ...change }));
  };
  const { kind, who, name, environment, namespace, table, column, expression } = fields;
  // Judged as the request names it: a typed name is sent trimmed.
  const managed = environment === null ? null : managedScopeOf(scopes, environment.trim());
  const held = missing(t, fields);
  const listed = environments ?? [];
  const create = async (): Promise<void> => {
    const body: RowFilterCreateIn = {
      principal: who === "principal" ? name.trim() : null,
      group: who === "group" ? name.trim() : null,
      environment: (environment as string).trim(),
      namespace: namespace.trim(),
      table: table.trim(),
      expression,
    };
    await (kind === "column-mask"
      ? createColumnMask({ ...body, column: column.trim() })
      : createRowFilter(body));
    onCreated(kind);
  };
  const close = (): void => {
    if (!attempt.busy) onClose();
  };
  return (
    <ActionDialog onClose={close}>
      <Form
        onSubmit={(event) => {
          // Create is disabled while the form is held, absent in a managed environment and
          // pending while the request runs, so none of them submits the form.
          event.preventDefault();
          attempt.run(create);
        }}
      >
        <Heading slot="title">{t("policies.addDialog.title")}</Heading>
        <RadioGroup
          className="field"
          orientation="horizontal"
          value={kind}
          onChange={(value) => {
            set({ kind: value as PolicyKind });
          }}
        >
          <Label>{t("policies.addDialog.kind")}</Label>
          <RadioField value="row-filter" className="radio-field">
            <RadioButton className="radio">{t("policies.addDialog.rowFilter")}</RadioButton>
          </RadioField>
          <RadioField value="column-mask" className="radio-field">
            <RadioButton className="radio">{t("policies.addDialog.columnMask")}</RadioButton>
          </RadioField>
        </RadioGroup>
        <RadioGroup
          className="field"
          orientation="horizontal"
          value={who}
          onChange={(value) => {
            set({ who: value as Fields["who"] });
          }}
        >
          <Label>{t("policies.addDialog.for")}</Label>
          <RadioField value="principal" className="radio-field">
            <RadioButton className="radio">{t("policies.addDialog.person")}</RadioButton>
          </RadioField>
          <RadioField value="group" className="radio-field">
            <RadioButton className="radio">{t("policies.addDialog.group")}</RadioButton>
          </RadioField>
        </RadioGroup>
        <PlainField
          label={t("policies.addDialog.name")}
          value={name}
          onChange={(value) => {
            set({ name: value });
          }}
        />
        {environments !== null && environments.length === 0 ? (
          <PlainField
            label={t("policies.addDialog.environment")}
            value={environment ?? ""}
            help={t("policies.addDialog.environmentHelp")}
            onChange={(value) => {
              set({ environment: value.trim() === "" ? null : value });
            }}
          />
        ) : (
          <Choice<string>
            label={t("policies.addDialog.environment")}
            value={environment}
            options={listed.map((choice) => ({
              id: choice.name,
              text: markedText(choice.name),
            }))}
            onChange={(id) => {
              set({ environment: id });
            }}
            render={(id) => (
              <span>
                <EnvironmentName
                  environment={listed.find((choice) => choice.name === id) as EnvironmentChoice}
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
        <PlainField
          label={t("policies.addDialog.namespace")}
          value={namespace}
          help={t("policies.addDialog.namespaceHelp")}
          onChange={(value) => {
            set({ namespace: value });
          }}
        />
        <PlainField
          label={t("policies.addDialog.table")}
          value={table}
          onChange={(value) => {
            set({ table: value });
          }}
        />
        {kind === "column-mask" && (
          <PlainField
            label={t("policies.addDialog.column")}
            value={column}
            onChange={(value) => {
              set({ column: value });
            }}
          />
        )}
        <TextField
          className="field"
          value={expression}
          isRequired
          onChange={(value) => {
            set({ expression: value });
          }}
        >
          <Label>{t("policies.addDialog.expression")}</Label>
          <TextArea className="expression-input" autoComplete="off" spellCheck={false} />
          <Text slot="description">
            {kind === "column-mask"
              ? t("policies.addDialog.maskHelp")
              : t("policies.addDialog.filterHelp")}
          </Text>
        </TextField>
        {managed !== null && (
          <p className="page-notice">
            <GitMark />{" "}
            {t("policies.addDialog.managed", {
              environment: managed.scope,
              file: managed.file_name,
            })}
          </p>
        )}
        <FormFoot
          attempt={attempt}
          submit={t("policies.addDialog.create")}
          held={held}
          blocked={managed !== null}
          onCancel={close}
        />
      </Form>
    </ActionDialog>
  );
}
