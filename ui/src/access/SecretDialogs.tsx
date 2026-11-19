/**
 * The dialogs of Settings › Access › Secrets: Add secret, Rotate, Set use and Delete, loaded
 * when one is opened.
 *
 * @remarks
 * A secret's values live only in the state of the dialog that asks for them: they are sent in
 * one request's body, never shown again, and gone when the dialog closes. Adding, rotating and
 * setting the use ask for the passkey touch before the request; deleting needs none.
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import {
  Button,
  FieldError,
  Form,
  Heading,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  Popover,
  Select,
  SelectValue,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";

import {
  bindSecret,
  createSecret,
  revokeSecret,
  rotateSecret,
  type SecretOut,
  type SecretUseIn,
  type SecretUseKind,
} from "../api/secrets";
import { ActionDialog } from "../shell/ActionDialog";
import { PendingButton } from "../shell/PendingButton";
import { AttemptStatus, useAttempt } from "../sign-in/Attempt";
import {
  newRow,
  SecretValuesFields,
  useValueInputs,
  type ValueRow,
  valuesReady,
  withKeys,
} from "./SecretValuesFields";
import { secretUseText } from "./secretUse";
import { TypedName } from "./TypedName";
import "./secretStrings";

/** Properties of each secret dialog. */
export interface SecretDialogProps {
  /** The environment the secret lives in. */
  readonly environment: string;
  /** Called once the change is made. */
  readonly onDone: () => void;
  /** Closes the dialog without a change. */
  readonly onClose: () => void;
}

/** Properties of the dialogs of one secret: Rotate, Set use and Delete. */
export interface OneSecretDialogProps extends SecretDialogProps {
  /** The secret. */
  readonly secret: SecretOut;
}

/** A use as the form holds it: no use, or a kind and the name of its target. */
interface UseChoice {
  /** The kind, or null for none. */
  readonly kind: SecretUseKind | null;
  /** The target's name, for a kind that has one. */
  readonly target: string;
}

/** The kinds of use, in the order the form offers them. */
const KINDS: readonly SecretUseKind[] = [
  "package_index",
  "repository",
  "connection",
  "environment",
];

/** The keys a use's how-to names, which choosing it fills into empty keys. */
const KEYS_OF: Readonly<Partial<Record<SecretUseKind, readonly string[]>>> = {
  package_index: ["username", "password"],
  repository: ["token"],
};

/**
 * The use to send, or null for none.
 *
 * @param choice - The form's use.
 * @returns The use.
 */
function chosenUse(choice: UseChoice): SecretUseIn | null {
  if (choice.kind === null) return null;
  return {
    kind: choice.kind,
    target: choice.kind === "environment" ? null : choice.target.trim(),
  };
}

/**
 * Whether a use is complete: none, the environment, or a kind with its target named.
 *
 * @param choice - The form's use.
 * @returns True when it is.
 */
function choiceComplete(choice: UseChoice): boolean {
  return choice.kind === null || choice.kind === "environment" || choice.target.trim() !== "";
}

/** Properties of {@link UseFields}. */
interface UseFieldsProps {
  /** The use. */
  readonly value: UseChoice;
  /** Called with the use as changed. */
  readonly onChange: (value: UseChoice) => void;
  /** Whether None is offered. */
  readonly offerNone: boolean;
  /** Whether a send was tried, so a missing target shows. */
  readonly checked: boolean;
}

/**
 * The use: a kind, then the name of its target, except for the environment.
 *
 * @param props - The use, the handler, whether None is offered and whether a send was tried.
 * @param props.value - The use.
 * @param props.onChange - Called with the use as changed.
 * @param props.offerNone - Whether None is offered.
 * @param props.checked - Whether a send was tried.
 * @returns The fields.
 */
function UseFields({ value, onChange, offerNone, checked }: UseFieldsProps): JSX.Element {
  const { t } = useTranslation();
  const options = offerNone ? (["none", ...KINDS] as const) : KINDS;
  const kind = value.kind;
  return (
    <>
      <Select
        className="env-picker"
        value={kind ?? (offerNone ? "none" : null)}
        placeholder={t("secrets.useDialog.chooseFirst")}
        onChange={(key) => {
          const next = String(key);
          onChange({ ...value, kind: next === "none" ? null : (next as SecretUseKind) });
        }}
      >
        <Label>{t("secrets.useField.label")}</Label>
        <Button className="button">
          <SelectValue />
          <span aria-hidden="true">▾</span>
        </Button>
        {offerNone && <Text slot="description">{t("secrets.useField.help")}</Text>}
        <Popover className="float" placement="bottom start">
          <ListBox className="menu">
            {options.map((option) => (
              <ListBoxItem
                key={option}
                id={option}
                className="menu-item"
                textValue={t(`secrets.useField.${option}`)}
              >
                <span className="menu-check" aria-hidden="true" />
                {t(`secrets.useField.${option}`)}
              </ListBoxItem>
            ))}
          </ListBox>
        </Popover>
      </Select>
      {kind !== null && kind !== "environment" && (
        <TextField
          className="field"
          value={value.target}
          isInvalid={checked && value.target.trim() === ""}
          onChange={(target) => {
            onChange({ ...value, target });
          }}
        >
          <Label>
            {t("secrets.useField.target", { kind: t(`secrets.useField.targetKinds.${kind}`) })}
          </Label>
          <Input autoComplete="off" spellCheck={false} />
          <FieldError>
            {t("secrets.useField.targetMissing", {
              kind: t(`secrets.useField.targetKinds.${kind}`),
            })}
          </FieldError>
        </TextField>
      )}
    </>
  );
}

/** Properties of {@link Actions}. */
interface ActionsProps {
  /** The submit button's label. */
  readonly label: string;
  /** Whether the request is on its way. */
  readonly busy: boolean;
  /** Closes the dialog. */
  readonly onClose: () => void;
}

/**
 * The dialog's buttons: the one that sends, and Cancel.
 *
 * @param props - The label, the wait and the close handler.
 * @param props.label - The submit button's label.
 * @param props.busy - Whether the request is on its way.
 * @param props.onClose - Closes the dialog.
 * @returns The buttons.
 */
function Actions({ label, busy, onClose }: ActionsProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div className="step-actions">
      <PendingButton type="submit" className="button button-primary" pending={busy}>
        {label}
      </PendingButton>
      <Button className="button" onPress={onClose}>
        {t("dialog.cancel")}
      </Button>
    </div>
  );
}

/**
 * Add secret: its name, its values and, optionally, its use. Choosing a package index fills
 * the keys `username` and `password` into empty keys, a repository `token`. A refusal keeps
 * every field, the values too, so the person can correct and send again.
 *
 * @param props - The environment and the handlers.
 * @param props.environment - The environment.
 * @param props.onDone - Called once the secret is made.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function AddSecretDialog({ environment, onDone, onClose }: SecretDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [name, setName] = useState("");
  const [rows, setRows] = useState<readonly ValueRow[]>(() => [newRow()]);
  const inputs = useValueInputs();
  const [use, setUse] = useState<UseChoice>({ kind: null, target: "" });
  const [checked, setChecked] = useState(false);
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          setChecked(true);
          if (name.trim() === "" || !valuesReady(t, rows) || !choiceComplete(use)) return;
          attempt.run(async () => {
            await createSecret(environment, {
              name: name.trim(),
              values: inputs.read(rows),
              use: chosenUse(use),
            });
            onDone();
          });
        }}
      >
        <Heading slot="title">{t("secrets.addDialog.title", { environment })}</Heading>
        <TextField
          className="field"
          isRequired
          value={name}
          onChange={setName}
          isInvalid={checked && name.trim() === ""}
        >
          <Label>{t("secrets.addDialog.name")}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t("secrets.addDialog.nameHelp")}</Text>
          <FieldError />
        </TextField>
        <SecretValuesFields rows={rows} onChange={setRows} checked={checked} inputs={inputs} />
        <UseFields
          value={use}
          offerNone
          checked={checked}
          onChange={(next) => {
            setUse(next);
            const keys = next.kind === null ? undefined : KEYS_OF[next.kind];
            if (keys !== undefined) setRows((previous) => withKeys(previous, keys));
          }}
        />
        <Actions label={t("secrets.addDialog.submit")} busy={attempt.busy} onClose={onClose} />
        <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      </Form>
    </ActionDialog>
  );
}

/**
 * Rotate: the secret's new values, which replace all of its values; the keys its use reads (a
 * package index's `username` and `password`, a repository's `token`) are filled in. What uses
 * the secret picks them up.
 *
 * @param props - The environment, the secret and the handlers.
 * @param props.environment - The environment.
 * @param props.secret - The secret.
 * @param props.onDone - Called once the values are replaced.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function RotateSecretDialog({
  environment,
  secret,
  onDone,
  onClose,
}: OneSecretDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const { name } = secret;
  // The values replace every value the secret has: start from the keys its use reads.
  const [rows, setRows] = useState<readonly ValueRow[]>(() => {
    const keys = secret.use === null ? undefined : KEYS_OF[secret.use];
    return keys === undefined ? [newRow()] : keys.map((key) => newRow(key));
  });
  const inputs = useValueInputs();
  const [checked, setChecked] = useState(false);
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          setChecked(true);
          if (!valuesReady(t, rows)) return;
          attempt.run(async () => {
            await rotateSecret(environment, name, inputs.read(rows));
            onDone();
          });
        }}
      >
        <Heading slot="title">{t("secrets.rotateDialog.title", { name })}</Heading>
        <p>{t("secrets.rotateDialog.intro")}</p>
        <SecretValuesFields rows={rows} onChange={setRows} checked={checked} inputs={inputs} />
        <Actions label={t("secrets.rotateDialog.submit")} busy={attempt.busy} onClose={onClose} />
        <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      </Form>
    </ActionDialog>
  );
}

/**
 * Set use: what the secret may be used for, starting from its use now.
 *
 * @param props - The environment, the secret and the handlers.
 * @param props.environment - The environment.
 * @param props.secret - The secret.
 * @param props.onDone - Called once the use is set.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function SecretUseDialog({
  environment,
  secret,
  onDone,
  onClose,
}: OneSecretDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const { name } = secret;
  const [use, setUse] = useState<UseChoice>({
    kind: secret.use,
    target: secret.use_target ?? "",
  });
  const [checked, setChecked] = useState(false);
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          setChecked(true);
          const chosen = chosenUse(use);
          if (chosen === null || !choiceComplete(use)) return;
          attempt.run(async () => {
            await bindSecret(environment, name, chosen);
            onDone();
          });
        }}
      >
        <Heading slot="title">{t("secrets.useDialog.title", { name })}</Heading>
        <UseFields value={use} onChange={setUse} offerNone={false} checked={checked} />
        {checked && use.kind === null && (
          <p className="step-error" role="alert">
            {t("secrets.useDialog.chooseFirst")}
          </p>
        )}
        <Actions label={t("secrets.useDialog.submit")} busy={attempt.busy} onClose={onClose} />
        <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      </Form>
    </ActionDialog>
  );
}

/**
 * Delete: held until the secret's name is typed exactly; with a use set, says what stops
 * working. Needs no passkey touch.
 *
 * @param props - The environment, the secret and the handlers.
 * @param props.environment - The environment.
 * @param props.secret - The secret.
 * @param props.onDone - Called once the secret is deleted.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function DeleteSecretDialog({
  environment,
  secret,
  onDone,
  onClose,
}: OneSecretDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const { name } = secret;
  const [typed, setTyped] = useState("");
  const matches = typed === name;
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          if (!matches || attempt.busy) return;
          attempt.run(async () => {
            await revokeSecret(environment, name);
            onDone();
          });
        }}
      >
        <Heading slot="title">{t("secrets.deleteDialog.title", { name })}</Heading>
        <p>{t("secrets.deleteDialog.intro")}</p>
        {secret.use !== null && (
          <p className="page-notice">
            {t("secrets.deleteDialog.inUse", { use: secretUseText(t, secret) })}
          </p>
        )}
        <TypedName
          label={t("secrets.deleteDialog.type", { name })}
          typed={typed}
          onTyped={setTyped}
          matches={matches}
          submit={t("secrets.deleteDialog.submit")}
          held={t("secrets.deleteDialog.held")}
          busy={attempt.busy}
          onCancel={onClose}
        />
        <AttemptStatus attempt={attempt} waiting={t("common.working")} />
      </Form>
    </ActionDialog>
  );
}
