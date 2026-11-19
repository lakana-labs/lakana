/**
 * The dialogs of Settings › Environments: Create environment; an environment's Set retention,
 * Change storage and Delete environment; and a deleted environment's Restore, Delete the files
 * and Remove from the catalog. They load on demand, when one is opened.
 *
 * @remarks
 * A storage option's value can be an inline credential. It is typed into an uncontrolled
 * password field and read from it only when the form sends (`SecretValuesFields`): it is never
 * React state, never in the address, a storage, a log, an error or an accessible name, and it is
 * gone with the field when the dialog closes.
 * @packageDocumentation
 */
import { type JSX, useCallback, useState } from "react";
import {
  CheckboxButton,
  CheckboxField,
  FieldError,
  Form,
  Heading,
  Input,
  Label,
  NumberField,
  RadioButton,
  RadioField,
  RadioGroup,
  Text,
  TextField,
} from "react-aria-components";
import { useTranslation } from "react-i18next";
import type { NavigateOptions } from "react-router";

import {
  changeStorage,
  createEnvironment,
  deleteEnvironment,
  type EnvironmentOut,
  getStorage,
  purgeEnvironmentRows,
  restoreEnvironment,
  setEnvironmentRetention,
  type StorageScheme,
} from "../api/environments";
import { listSecrets } from "../api/secrets";
import { Choice } from "../access/Choice";
import { FormFoot } from "../access/FormFoot";
import "../access/secrets.css";
import {
  SecretValuesFields,
  useValueInputs,
  type ValueRow,
  valuesReady,
} from "../access/SecretValuesFields";
import { ConfirmRow, useConfirm } from "../catalog/actions/ConfirmRow";
import { ReadFailed } from "../catalog/ReadFailed";
import { ConfirmDialog } from "../shell/ConfirmDialog";
import { ActionDialog } from "../shell/ActionDialog";
import { useResource } from "../shell/useResource";
import { markedText, VisibleText } from "../shell/VisibleText";
import { useAttempt } from "../sign-in/Attempt";
import "./environmentDialogStrings";
import { ENVIRONMENTS_PATH, environmentPath } from "./environmentPaths";
import { useSettingsViewer } from "./viewer";

const D = "settings.environmentDialogs";

/** The storage kinds a new environment may use, in the order offered. */
const SCHEMES: readonly StorageScheme[] = ["file", "s3", "abfs"];

/** The strings of the option rows. */
export const OPTION_WORDS = `${D}.options`;

/** What a page shows after it was created here: the next step. */
export interface CreatedState {
  /** Set by Create environment. */
  readonly created: true;
}

/**
 * What is wrong with a name the client can tell, or null: the server's 422 stays the authority.
 *
 * @param name - The name typed.
 * @returns The i18n key of the problem, or null.
 */
function nameProblem(name: string): string | null {
  if (name.trim() === "") return `${D}.create.nameMissing`;
  return name.includes(".") ? `${D}.create.nameDot` : null;
}

/** Properties of {@link CreateEnvironmentDialog}. */
export interface CreateEnvironmentDialogProps {
  /** Closes the dialog without a change. */
  readonly onClose: () => void;
  /** Closes the dialog and goes to the address (`useLeave`). */
  readonly onLeave: (to: string, options: NavigateOptions) => void;
}

/**
 * Create environment: its name, storage kind and root, its options and whether it is protected.
 * It asks for the passkey touch first when the last one is old, and goes to the new
 * environment's page; the server's refusal stays here with every field kept.
 *
 * @param props - The handlers.
 * @param props.onClose - Closes the dialog.
 * @param props.onLeave - Closes the dialog and goes to the new environment's page.
 * @returns The dialog.
 */
export function CreateEnvironmentDialog({
  onClose,
  onLeave,
}: CreateEnvironmentDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const inputs = useValueInputs();
  const [name, setName] = useState("");
  const [nameLeft, setNameLeft] = useState(false);
  const [scheme, setScheme] = useState<StorageScheme>("file");
  const [root, setRoot] = useState("");
  const [rows, setRows] = useState<readonly ValueRow[]>([]);
  const [checked, setChecked] = useState(false);
  const [isProtected, setProtected] = useState(false);
  const c = `${D}.create`;
  const problem = nameLeft ? nameProblem(name) : null;
  const held = nameProblem(name) !== null || root.trim() === "" ? t(`${c}.held`) : null;
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          setChecked(true);
          if (!valuesReady(t, rows)) return;
          const created = name.trim();
          attempt.run(async () => {
            await createEnvironment({
              name: created,
              storage: { scheme, root: root.trim(), options: inputs.read(rows) },
              is_protected: isProtected,
            });
            const state: CreatedState = { created: true };
            onLeave(environmentPath(created), { state });
          });
        }}
      >
        <Heading slot="title">{t(`${c}.title`)}</Heading>
        <TextField
          className="field"
          value={name}
          isInvalid={problem !== null}
          onChange={setName}
          onBlur={() => {
            setNameLeft(true);
          }}
        >
          <Label>{t(`${c}.name`)}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t(`${c}.nameHelp`)}</Text>
          <FieldError>{problem === null ? null : t(problem)}</FieldError>
        </TextField>
        <RadioGroup
          className="field"
          value={scheme}
          onChange={(value) => {
            setScheme(value as StorageScheme);
          }}
        >
          <Label>{t(`${c}.kind`)}</Label>
          {SCHEMES.map((kind) => (
            <RadioField key={kind} value={kind} className="radio-field">
              <RadioButton className="radio">{t(`${c}.kinds.${kind}`)}</RadioButton>
            </RadioField>
          ))}
        </RadioGroup>
        <TextField className="field" value={root} onChange={setRoot}>
          <Label>{t(`${c}.root`)}</Label>
          <Input autoComplete="off" spellCheck={false} />
          <Text slot="description">{t(`${c}.examples.${scheme}`)}</Text>
        </TextField>
        <SecretValuesFields
          rows={rows}
          onChange={setRows}
          checked={checked}
          inputs={inputs}
          words={OPTION_WORDS}
          mayBeNone
        />
        <CheckboxField isSelected={isProtected} onChange={setProtected}>
          <CheckboxButton className="checkbox">{t(`${c}.protect`)}</CheckboxButton>
        </CheckboxField>
        <FormFoot
          attempt={attempt}
          submit={t(`${c}.submit`)}
          held={held}
          blocked={false}
          onCancel={onClose}
        />
      </Form>
    </ActionDialog>
  );
}

/** Properties of the dialogs of an environment's page. */
export interface EnvironmentDialogProps {
  /** The environment. */
  readonly environment: EnvironmentOut;
  /** Called once the change is made: the page reads the environment again. */
  readonly onDone: () => void;
  /** Closes the dialog without a change. */
  readonly onClose: () => void;
}

/** The most days a retention may be, as the server takes it. */
const MAX_RETENTION_DAYS = 3650;

/**
 * Set retention: keep the tables that set no retention of their own, or let them expire a number
 * of days after their last write or read. No passkey touch; the server's refusal (a protected
 * environment, days out of range) stays here with the days kept.
 *
 * @param props - The environment and what to call.
 * @param props.environment - The environment.
 * @param props.onDone - Called once it is saved.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function RetentionDialog({
  environment,
  onDone,
  onClose,
}: EnvironmentDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  // A protected environment's tables never expire: the server takes only turning it off.
  const onlyOff = environment.is_protected;
  const [choice, setChoice] = useState<"keep" | "expire">(
    environment.retention_days === null || onlyOff ? "keep" : "expire",
  );
  const [days, setDays] = useState(environment.retention_days ?? 30);
  const r = `${D}.retention`;
  const valid = Number.isInteger(days) && days >= 1 && days <= MAX_RETENTION_DAYS;
  return (
    <ActionDialog onClose={onClose}>
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          attempt.run(async () => {
            await setEnvironmentRetention(environment.name, choice === "keep" ? null : days);
            onDone();
          });
        }}
      >
        <Heading slot="title">{t(`${r}.title`, { name: markedText(environment.name) })}</Heading>
        <RadioGroup
          className="field"
          value={choice}
          onChange={(value) => {
            setChoice(value as "keep" | "expire");
          }}
        >
          <Label>{t(`${r}.rule`)}</Label>
          <RadioField value="keep" className="radio-field">
            <RadioButton className="radio">{t(`${r}.keep`)}</RadioButton>
          </RadioField>
          {!onlyOff && (
            <RadioField value="expire" className="radio-field">
              <RadioButton className="radio">{t(`${r}.expire`)}</RadioButton>
            </RadioField>
          )}
        </RadioGroup>
        {onlyOff && <p>{t(`${r}.onlyOff`)}</p>}
        {choice === "expire" && (
          <NumberField
            className="field"
            value={days}
            onChange={setDays}
            minValue={1}
            maxValue={MAX_RETENTION_DAYS}
            formatOptions={{ useGrouping: false }}
          >
            <Label>{t(`${r}.days`)}</Label>
            <Input />
            <Text slot="description">{t(`${r}.daysHelp`)}</Text>
          </NumberField>
        )}
        <FormFoot
          attempt={attempt}
          submit={t(`${r}.save`)}
          held={choice === "expire" && !valid ? t(`${r}.held`) : null}
          blocked={false}
          onCancel={onClose}
        />
      </Form>
    </ActionDialog>
  );
}

/** The secret picker's choice that keeps the current secret; a secret's is `secret:` and its name. */
const KEEP = "keep";

/** Properties of {@link SecretChoice}. */
interface SecretChoiceProps {
  /** The environment. */
  readonly environment: string;
  /** The choice: {@link KEEP}, or `secret:` and a name. */
  readonly value: string;
  /** Called with the choice. */
  readonly onChange: (value: string) => void;
}

/**
 * The environment's secrets to choose the storage secret from, with Keep the current secret
 * first.
 *
 * @param props - The environment, the choice and the handler.
 * @param props.environment - The environment.
 * @param props.value - The choice.
 * @param props.onChange - Called with the choice.
 * @returns The picker, or why it cannot be offered.
 */
function SecretChoice({ environment, value, onChange }: SecretChoiceProps): JSX.Element {
  const { t } = useTranslation();
  const load = useCallback(() => listSecrets(environment), [environment]);
  const read = useResource(load);
  const s = `${D}.storage`;
  if (read.data === undefined) {
    return read.error === null ? (
      <p className="step-status">{t(`${s}.reading`)}</p>
    ) : (
      <ReadFailed message={read.error} onRetry={read.reload} />
    );
  }
  if (read.data.length === 0) return <p className="step-status">{t(`${s}.secretNoneHere`)}</p>;
  return (
    <Choice<string>
      label={t(`${s}.secret`)}
      value={value}
      options={[
        { id: KEEP, text: t(`${s}.keepSecret`) },
        ...read.data.map((secret) => ({ id: `secret:${secret.name}`, text: secret.name })),
      ]}
      onChange={onChange}
    />
  );
}

/**
 * Change storage: the storage secret, chosen from the environment's secrets, and the options,
 * kept as they are or replaced whole. Without the right to manage secrets in the environment the
 * secret is not offered (a sentence names the right), and the page offers no Change storage at
 * all for a storage with a secret, whose options need that right too. It names the options set now by their keys (never a value), asks for the passkey touch first when the
 * last one is old, and sends only what changes; the server's refusal stays here.
 *
 * @param props - The environment and what to call.
 * @param props.environment - The environment.
 * @param props.onDone - Called once it is changed.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function ChangeStorageDialog({
  environment,
  onDone,
  onClose,
}: EnvironmentDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const inputs = useValueInputs();
  // Change storage is offered once the viewer is read, so where one manages secrets is known.
  const managed = useSettingsViewer().secretEnvironments as readonly string[];
  const picks = managed.includes(environment.name);
  const load = useCallback(() => getStorage(environment.name), [environment.name]);
  const storage = useResource(load);
  const [secret, setSecret] = useState(KEEP);
  const [replace, setReplace] = useState(false);
  const [rows, setRows] = useState<readonly ValueRow[]>([]);
  const [checked, setChecked] = useState(false);
  const s = `${D}.storage`;
  const chosen = secret === KEEP ? "" : secret.slice("secret:".length);
  const secretRef = chosen === "" || chosen === environment.storage_secret_ref ? null : chosen;
  const keys = storage.data?.option_keys;
  return (
    <ActionDialog onClose={onClose}>
      <Form
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault();
          setChecked(true);
          if (replace && !valuesReady(t, rows)) return;
          attempt.run(async () => {
            await changeStorage(environment.name, {
              ...(secretRef === null ? {} : { secret_ref: secretRef }),
              ...(replace ? { options: inputs.read(rows) } : {}),
            });
            onDone();
          });
        }}
      >
        <Heading slot="title">{t(`${s}.title`, { name: markedText(environment.name) })}</Heading>
        <dl className="settings-facts">
          <dt>{t(`${s}.root`)}</dt>
          <dd className="environment-storage">
            <VisibleText text={environment.storage_root} />
          </dd>
          <dt>{t(`${s}.secret`)}</dt>
          <dd>
            {environment.storage_secret_ref === null ? (
              t(`${s}.noSecretYet`)
            ) : (
              <VisibleText text={environment.storage_secret_ref} />
            )}
          </dd>
        </dl>
        {picks ? (
          <SecretChoice environment={environment.name} value={secret} onChange={setSecret} />
        ) : (
          <p>{t(`${s}.secretNeedsRight`, { name: markedText(environment.name) })}</p>
        )}

        <RadioGroup
          className="field"
          value={replace ? "replace" : "keep"}
          onChange={(value) => {
            setReplace(value === "replace");
          }}
        >
          <Label>{t(`${s}.options`)}</Label>
          <RadioField value="keep" className="radio-field">
            <RadioButton className="radio">{t(`${s}.keepOptions`)}</RadioButton>
          </RadioField>
          <RadioField value="replace" className="radio-field">
            <RadioButton className="radio">{t(`${s}.replaceOptions`)}</RadioButton>
          </RadioField>
        </RadioGroup>
        {keys === undefined ? (
          storage.error === null ? (
            <p className="step-status">{t(`${s}.reading`)}</p>
          ) : (
            <ReadFailed message={storage.error} onRetry={storage.reload} />
          )
        ) : (
          <p className="step-hint">
            {keys.length === 0 ? t(`${s}.noneSetNow`) : t(`${s}.setNow`, { keys: keys.join(", ") })}
          </p>
        )}
        {replace && (
          <>
            <p className="step-hint">{t(`${s}.replaceHelp`)}</p>
            <SecretValuesFields
              rows={rows}
              onChange={setRows}
              checked={checked}
              inputs={inputs}
              words={OPTION_WORDS}
              mayBeNone
            />
          </>
        )}
        <FormFoot
          attempt={attempt}
          submit={t(`${s}.submit`)}
          held={secretRef === null && !replace ? t(`${s}.held`) : null}
          blocked={false}
          onCancel={onClose}
        />
      </Form>
    </ActionDialog>
  );
}

/** What the list shows after an environment was deleted here: what became of it. */
export interface DeletedState {
  /** The deletion. */
  readonly deleted: {
    /** The environment's name. */
    readonly name: string;
    /** Until when it can be restored; null when its files go at the next clean-up. */
    readonly restorableUntil: string | null;
  };
}

/** Properties of {@link TypedName}. */
interface TypedNameProps {
  /** Its label, naming what to type. */
  readonly label: string;
  /** What was typed. */
  readonly value: string;
  /** Called with what is typed. */
  readonly onChange: (value: string) => void;
}

/**
 * The field the name is typed into before something that cannot be taken back.
 *
 * @param props - The label, the value and the handler.
 * @param props.label - Its label.
 * @param props.value - What was typed.
 * @param props.onChange - Called with what is typed.
 * @returns The field.
 */
function TypedName({ label, value, onChange }: TypedNameProps): JSX.Element {
  return (
    <TextField className="field" value={value} onChange={onChange}>
      <Label>{label}</Label>
      <Input autoComplete="off" spellCheck={false} />
    </TextField>
  );
}

/** Properties of {@link DeleteEnvironmentDialog}. */
export interface DeleteEnvironmentDialogProps {
  /** The environment, not protected. */
  readonly environment: EnvironmentOut;
  /** Closes the dialog without a change. */
  readonly onClose: () => void;
  /** Closes the dialog and goes to the address (`useLeave`). */
  readonly onLeave: (to: string, options: NavigateOptions) => void;
}

/**
 * Delete environment: what it does, the name typed, and whether its files are kept for the undo
 * period (the default, with no passkey touch) or deleted at the next clean-up (after the touch;
 * it cannot be restored then). Success goes to the list, which says what became of it; the
 * server's refusal (a repository still deployed) stays here.
 *
 * @param props - The environment and the handlers.
 * @param props.environment - The environment.
 * @param props.onClose - Closes the dialog.
 * @param props.onLeave - Closes the dialog and goes to the list.
 * @returns The dialog.
 */
export function DeleteEnvironmentDialog({
  environment,
  onClose,
  onLeave,
}: DeleteEnvironmentDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [typed, setTyped] = useState("");
  const [purge, setPurge] = useState(false);
  const name = markedText(environment.name);
  const d = `${D}.delete`;
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: typed !== environment.name,
    onConfirm: () => {
      attempt.run(async () => {
        const deleted = await deleteEnvironment(environment.name, purge);
        const state: DeletedState = {
          deleted: {
            name: environment.name,
            restorableUntil: purge ? null : deleted.purge_after,
          },
        };
        onLeave(ENVIRONMENTS_PATH, { replace: true, state });
      });
    },
  });
  return (
    <ActionDialog onClose={onClose}>
      <Heading slot="title">{t(`${d}.title`, { name })}</Heading>
      <p>{t(`${d}.body`)}</p>
      <RadioGroup
        className="field"
        value={purge ? "purge" : "keep"}
        onChange={(value) => {
          setPurge(value === "purge");
        }}
      >
        <Label>{t(`${d}.files`)}</Label>
        <RadioField value="keep" className="radio-field">
          <RadioButton className="radio">{t(`${d}.keep`)}</RadioButton>
        </RadioField>
        <RadioField value="purge" className="radio-field">
          <RadioButton className="radio">{t(`${d}.purge`)}</RadioButton>
        </RadioField>
      </RadioGroup>
      <TypedName label={t(`${d}.type`, { name })} value={typed} onChange={setTyped} />
      <ConfirmRow attempt={attempt} label={t(`${d}.submit`)} confirm={confirm} onCancel={onClose} />
    </ActionDialog>
  );
}

/** What can be done to a deleted environment. */
export type DeletedAction = "restore" | "purgeFiles" | "removeRows";

/** Properties of {@link DeletedEnvironmentDialog}. */
export interface DeletedEnvironmentDialogProps extends EnvironmentDialogProps {
  /** What is done. */
  readonly action: DeletedAction;
}

/**
 * Delete the files of a deleted environment now: the name typed, then the passkey touch.
 *
 * @param props - The environment and what to call.
 * @param props.environment - The deleted environment.
 * @param props.onDone - Called once it is done.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
function PurgeFilesDialog({ environment, onDone, onClose }: EnvironmentDialogProps): JSX.Element {
  const { t } = useTranslation();
  const attempt = useAttempt();
  const [typed, setTyped] = useState("");
  const name = markedText(environment.name);
  const f = `${D}.purgeFiles`;
  const confirm = useConfirm({
    busy: attempt.busy,
    disabled: typed !== environment.name,
    onConfirm: () => {
      attempt.run(async () => {
        await deleteEnvironment(environment.name, true);
        onDone();
      });
    },
  });
  return (
    <ActionDialog onClose={onClose}>
      <Heading slot="title">{t(`${f}.title`, { name })}</Heading>
      <p>{t(`${f}.body`)}</p>
      <TypedName label={t(`${f}.type`, { name })} value={typed} onChange={setTyped} />
      <ConfirmRow attempt={attempt} label={t(`${f}.submit`)} confirm={confirm} onCancel={onClose} />
    </ActionDialog>
  );
}

/**
 * A deleted environment's action: Restore (no passkey touch), Delete the files now (the name
 * typed, then the touch) or Remove from the catalog (after the touch); a refusal stays here.
 *
 * @param props - The action, the environment and what to call.
 * @param props.action - What is done.
 * @param props.environment - The deleted environment.
 * @param props.onDone - Called once it is done.
 * @param props.onClose - Closes the dialog.
 * @returns The dialog.
 */
export function DeletedEnvironmentDialog({
  action,
  environment,
  onDone,
  onClose,
}: DeletedEnvironmentDialogProps): JSX.Element {
  const { t } = useTranslation();
  if (action === "purgeFiles") {
    return <PurgeFilesDialog environment={environment} onDone={onDone} onClose={onClose} />;
  }
  const k = `${D}.${action}`;
  return (
    <ConfirmDialog
      title={t(`${k}.title`, { name: markedText(environment.name) })}
      confirmLabel={t(`${k}.confirm`)}
      onConfirm={async () => {
        await (action === "restore"
          ? restoreEnvironment(environment.name)
          : purgeEnvironmentRows(environment.name));
      }}
      onCancel={onClose}
      onDone={onDone}
    >
      <p>{t(`${k}.body`)}</p>
    </ConfirmDialog>
  );
}
